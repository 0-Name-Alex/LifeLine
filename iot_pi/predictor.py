"""Reusable predictor shared by offline replay and the Raspberry Pi MQTT subscriber."""
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import joblib
import pandas as pd
from features import BASE_COLUMNS, STUCK_SAMPLES, features_from_log
from fall_detector import FallDetector

ROOT = Path(__file__).resolve().parent
RESET_GAP_S = 60  # no rolling features across collection gaps

class RiskPredictor:
    def __init__(self, model_dir=None, upright=None):
        model_dir = Path(model_dir or ROOT / 'models')
        self.classifier = joblib.load(model_dir / 'risk_classifier.joblib')
        self.anomaly = joblib.load(model_dir / 'anomaly_detector.joblib')
        self.meta = json.loads((model_dir / 'metadata.json').read_text(encoding='utf-8'))
        # Older metadata only had window_samples (logged every 2 s).
        self.window_s = float(self.meta.get('window_seconds', self.meta.get('window_samples', 10) * 2))
        self.buffers = {}  # separate histories for each worker
        self.fall = FallDetector(upright=upright, reset_gap_s=RESET_GAP_S)

    @staticmethod
    def _timestamp(data, now):
        """Use the packet's own timestamp (CSV replay / demo) if it has one, else arrival time."""
        ts = pd.to_datetime(data.get('timestamp'), errors='coerce') if data.get('timestamp') else pd.NaT
        if pd.isna(ts):
            return pd.Timestamp(now or datetime.now())
        return ts.tz_localize(None) if ts.tzinfo else ts

    def predict(self, packet: dict, now=None) -> dict:
        data = packet.copy()
        # MQTT sources use 'gas' and sometimes 'gas_raw'.
        if 'gas_raw' not in data:
            data['gas_raw'] = data.get('gas')
        if 'avg_bpm' not in data:
            data['avg_bpm'] = data.get('bpm')
        worker = str(data.get('worker_id', 'worker_1'))
        if any(data.get(k) is None for k in ('accel_x', 'accel_y', 'accel_z', 'gas_raw')):
            raise ValueError('Missing mandatory accelerometer or gas input')
        ts = self._timestamp(data, now)
        history = self.buffers.setdefault(worker, deque())
        if history and (ts - history[-1]['timestamp']).total_seconds() > RESET_GAP_S:
            history.clear()
        sample = {k: data.get(k) for k in BASE_COLUMNS}
        sample['timestamp'] = ts
        history.append(sample)
        # Keep the time window plus a few extra rows so frozen-sensor runs are still visible.
        while len(history) > STUCK_SAMPLES and (ts - history[0]['timestamp']).total_seconds() > self.window_s:
            history.popleft()
        frame = pd.DataFrame(history)
        frame['session'] = 1
        X = features_from_log(frame, window_seconds=self.window_s).iloc[[-1]]
        expected = self.meta['feature_names']
        X = X[expected]
        risk_int = int(self.classifier.predict(X)[0])
        prob_values = self.classifier.predict_proba(X)[0]
        probabilities = {str(int(label)): float(p) for label, p in zip(self.classifier.classes_, prob_values)}
        anomaly_flag = bool(self.anomaly.predict(X)[0] == -1)
        anomaly_score = float(-self.anomaly.decision_function(X)[0])
        fall = self.fall.update(worker, ts.timestamp(), data)
        missing = [k for k in BASE_COLUMNS if sample[k] is None or str(sample[k]).strip().lower() in ('', 'n/a', 'nan')]
        quality = []
        if missing:
            quality.append('missing:' + ','.join(missing))
        if not (str(data.get('finger', 0)).strip().lower() in ('1', '1.0', 'true')):
            quality.append('pulse_not_validated')
        quality.append('mpu_temp_is_chip_temperature')
        if fall['fall_reason'].startswith('accel_stuck'):
            quality.append('accel_stuck')
        if (ts - history[0]['timestamp']).total_seconds() < self.window_s * 0.75:
            quality.append('window_warming_up')
        return {
            'worker_id': worker,
            'risk_class': risk_int,
            'risk_label': self.meta['class_labels'][str(risk_int)],
            'risk_class_probabilities': probabilities,
            'anomaly': anomaly_flag,
            'anomaly_score': round(anomaly_score, 6),
            **fall,
            'quality_notes': quality,
            'ml_mode': 'experimental_advisory_only',
        }
