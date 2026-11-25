"""Shared training/inference feature calculation for the original jacket MQTT schema."""
from __future__ import annotations
import numpy as np
import pandas as pd

BASE_COLUMNS = ['accel_x', 'accel_y', 'accel_z', 'gas_raw', 'avg_bpm', 'finger', 'mpu_temp']
FEATURES = [
    'accel_x', 'accel_y', 'accel_z', 'accel_magnitude',
    'accel_mean_10', 'accel_std_10',
    'gas_raw', 'gas_mean_10', 'gas_std_10', 'gas_change_10',
    'mpu_temp', 'temp_mean_10', 'temp_change_10',
    'bpm_valid', 'valid_bpm', 'bpm_mean_10',
]
# Rolling windows are defined in SECONDS, not samples. The training CSV was logged
# every ~2 s, so 20 s = the original 10-sample window. The live ESP32 sends every
# 1 s; a sample-count window would silently cover only 10 s there.
WINDOW_SECONDS = 20
# Identical x/y/z for this many consecutive samples = frozen MPU6050 read (I2C fault).
STUCK_SAMPLES = 3

def validate_input(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in BASE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError('Missing sensor fields: ' + ', '.join(missing))
    out = df.copy()
    for c in BASE_COLUMNS:
        out[c] = pd.to_numeric(out[c], errors='coerce')
    # Missing readings stay NaN; they are NOT replaced with artificial zeroes.
    out.loc[~out['gas_raw'].between(0, 4095), 'gas_raw'] = np.nan
    out.loc[~out['mpu_temp'].between(-20, 85), 'mpu_temp'] = np.nan
    out.loc[~out['finger'].isin([0, 1]), 'finger'] = np.nan
    # MPU6050 maximum range is +/-16 g per axis.
    for c in ('accel_x', 'accel_y', 'accel_z'):
        out.loc[out[c].abs() > 16 * 9.81, c] = np.nan
    out['bpm_valid'] = ((out['finger'] == 1) & out['avg_bpm'].between(40, 220)).astype(int)
    out['valid_bpm'] = out['avg_bpm'].where(out['bpm_valid'] == 1)
    return out

def stuck_accel_mask(df: pd.DataFrame, n: int = STUCK_SAMPLES) -> pd.Series:
    """True where accel x/y/z has been exactly identical for n consecutive samples."""
    xyz = df[['accel_x', 'accel_y', 'accel_z']]
    session = df['session'] if 'session' in df.columns else pd.Series(1, index=df.index)
    same = xyz.eq(xyz.shift(1)).all(axis=1) & session.eq(session.shift(1))
    run_length = same.groupby((~same).cumsum()).transform('size')  # whole run, incl. its first row
    return run_length >= n

def _time_rolling(x: pd.DataFrame, col: str, window_s: int, how: str, min_periods: int = 1) -> pd.Series:
    out = pd.Series(np.nan, index=x.index, dtype=float)
    for _, idx in x.groupby('session', sort=False).groups.items():
        s = pd.Series(x.loc[idx, col].to_numpy(dtype=float), index=pd.DatetimeIndex(x.loc[idx, 'timestamp']))
        r = s.rolling(f'{window_s}s', min_periods=min_periods)
        if how == 'first':
            vals = r.apply(lambda a: a[0], raw=True)
        else:
            vals = getattr(r, how)()
        out.loc[idx] = vals.to_numpy()
    return out

def features_from_log(df: pd.DataFrame, window_seconds: int = WINDOW_SECONDS) -> pd.DataFrame:
    """One output row per input row. Needs a 'timestamp' column; rows must be time-sorted within each session.

    Feature names keep the historical '_10' suffix (= 10 samples at 2 s) so saved models stay compatible.
    """
    x = validate_input(df)
    if 'timestamp' not in x.columns:
        raise ValueError('features_from_log needs a timestamp column (windows are time-based)')
    x['timestamp'] = pd.to_datetime(x['timestamp'], errors='coerce')
    if x['timestamp'].isna().any():
        raise ValueError('Some timestamps could not be parsed')
    if 'session' not in x.columns:
        x['session'] = 1
    x.loc[stuck_accel_mask(x), ['accel_x', 'accel_y', 'accel_z']] = np.nan
    x['accel_magnitude'] = np.sqrt(x['accel_x']**2 + x['accel_y']**2 + x['accel_z']**2)
    w = window_seconds
    for col, prefix in [('accel_magnitude', 'accel'), ('gas_raw', 'gas'), ('mpu_temp', 'temp'), ('valid_bpm', 'bpm')]:
        x[f'{prefix}_mean_10'] = _time_rolling(x, col, w, 'mean')
        if prefix in ('accel', 'gas'):
            x[f'{prefix}_std_10'] = _time_rolling(x, col, w, 'std', min_periods=2).fillna(0)
    # Change since the oldest reading still inside the window; 0 until there is history.
    x['gas_change_10'] = (x['gas_raw'] - _time_rolling(x, 'gas_raw', w, 'first')).fillna(0)
    x['temp_change_10'] = (x['mpu_temp'] - _time_rolling(x, 'mpu_temp', w, 'first')).fillna(0)
    return x[FEATURES].copy()

def create_sessions(df: pd.DataFrame, gap_seconds: int = 60) -> pd.DataFrame:
    x = df.copy()
    x['timestamp'] = pd.to_datetime(x['timestamp'], errors='coerce')
    if x['timestamp'].isna().any():
        raise ValueError('Some timestamps could not be parsed')
    x = x.sort_values('timestamp', kind='stable').reset_index(drop=True)
    gaps = x['timestamp'].diff().dt.total_seconds()
    x['session'] = gaps.gt(gap_seconds).cumsum() + 1
    return x

def proxy_labels(df: pd.DataFrame) -> pd.Series:
    """WEAK LABELS computed by old threshold rules; NOT verified safety outcomes.

    0 normal; 1 warning; 2 high-risk. High-priority takes precedence.
    NOTE: these contain no fall/impact information at all; the accelerometer never produced a label.
    """
    high = (df['bpm_alert_status'].eq(1) | df['gas_status'].eq(2) | df['temp_status'].eq(1))
    warn = (df['gas_status'].eq(1) | df['temp_status'].eq(2))
    y = np.select([high, warn], [2, 1], default=0)
    return pd.Series(y, index=df.index, name='proxy_risk')
