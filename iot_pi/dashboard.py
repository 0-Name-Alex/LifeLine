"""
Local Health Monitor Dashboard (Flask)

Runs ALONGSIDE your existing Pi processor script. It does not talk to MQTT;
it simply follows the CSV file that script writes (health_monitor_log.csv),
so the alerts shown here are exactly the ones your script calculated.

Run:   python3 dashboard.py
Open:  http://<pi-ip>:5000   (e.g. http://10.142.95.249:5000)
"""

import csv
import io
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, Response

# --- Configuration (EDIT THESE VALUES) ---
# Must point to the same CSV your processor script writes.
# Your script uses a relative path, so this is the folder you START that script from.
CSV_FILE = os.environ.get("HEALTH_CSV", os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_monitor_log.csv"))

# Fall state written by live_inference.py (ok / impact / fall_suspected / man_down / recovered / unknown).
ML_CSV = os.environ.get("ML_CSV", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml_live_log.csv"))

DASHBOARD_VERSION = "v4 (workers by risk)"
WORKER_HIDE_AFTER_SEC = 600      # drop a worker from the risk board after 10 min without data
# Model classes -> board columns. A fall or man-down always goes to High.
RISK_COLUMN = {'Normal': 'low', 'Warning': 'medium', 'High Risk': 'high'}
PEAK_RE = re.compile(r'peak ([0-9.]+) g')

HOST = "0.0.0.0"   # reachable from any device on your Wi-Fi
PORT = 5000

MAX_ROWS_IN_MEMORY = 100_000   # ~27 hours at 1 reading/second
STALE_AFTER_SEC = 10           # show "sensor offline" if no new row for this long
MERGE_GAP_SEC = 15             # alerts closer together than this count as one episode

# Keep these in sync with your processor script (used for chart lines + zones)
BPM_THRESHOLD_HIGH = 80
GAS_THRESHOLD_WARN = 2000
GAS_THRESHOLD_DANGER = 3000
TEMP_THRESHOLD_HIGH = 38.0
TEMP_THRESHOLD_LOW = 28.0

# Fall / impact detection from the MPU6050 (values are in m/s^2)
GRAVITY = 9.81
IMPACT_THRESHOLD_G = 2.5      # |acceleration| above 2.5 g = possible fall/impact
FREEFALL_THRESHOLD_G = 0.4    # |acceleration| below 0.4 g = free fall

NUMERIC_FIELDS = ['accel_x', 'accel_y', 'accel_z', 'gas_raw', 'avg_bpm', 'finger',
                  'mpu_temp', 'bpm_alert_status', 'gas_status', 'temp_status']

app = Flask(__name__)
app.json.sort_keys = False     # keep dict order (heart rate zones etc.)

rows = deque(maxlen=MAX_ROWS_IN_MEMORY)
rows_lock = threading.Lock()
ml_rows = deque(maxlen=MAX_ROWS_IN_MEMORY)
ml_lock = threading.Lock()


# --- CSV follower (background thread) ---

def parse_row(raw):
    """Turn one CSV dict into numbers + derived values. Returns None if the row is broken."""
    try:
        r = {'ts': datetime.strptime(raw['timestamp'], '%Y-%m-%d %H:%M:%S')}
        for f in NUMERIC_FIELDS:
            r[f] = float(raw.get(f) or 0)
    except (ValueError, KeyError, TypeError):
        return None

    mag = math.sqrt(r['accel_x'] ** 2 + r['accel_y'] ** 2 + r['accel_z'] ** 2)
    r['accel_g'] = mag / GRAVITY
    if r['accel_g'] >= IMPACT_THRESHOLD_G:
        r['fall_status'] = 2          # impact
    elif 0 < r['accel_g'] <= FREEFALL_THRESHOLD_G:
        r['fall_status'] = 1          # free fall
    else:
        r['fall_status'] = 0
    return r


def parse_ml_row(raw):
    """One row of ml_live_log.csv. Its time is UTC, convert to the Pi's local time like the main CSV."""
    try:
        ts = datetime.fromisoformat(raw['timestamp_utc'])
        ts = ts.astimezone().replace(tzinfo=None) if ts.tzinfo else ts
        r = {'ts': ts, 'fall_state': raw.get('fall_state') or 'unknown',
             'fall_reason': raw.get('fall_reason') or '', 'risk_label': raw.get('risk_label') or '',
             'anomaly': str(raw.get('anomaly')).lower() == 'true'}
        for f in ('tilt_deg', 'still_s', 'accel_g', 'anomaly_score'):
            v = raw.get(f)
            r[f] = float(v) if v not in (None, '') else None
        # the impact row's reason carries the ESP32's 100 Hz peak, e.g. "peak 3.8 g"
        r['worker_id'] = raw.get('worker_id') or 'worker_1'
        for f in ('gas', 'avg_bpm', 'finger', 'mpu_temp'):
            v = raw.get(f)
            try:
                r[f] = float(v) if v not in (None, '', 'None') else None
            except ValueError:
                r[f] = None
        m = PEAK_RE.search(r['fall_reason'])
        r['peak_g'] = float(m.group(1)) if m else None
        return r
    except (ValueError, KeyError, TypeError):
        return None


def follow_csv(path=None, parse=None, target=None, lock=None):
    """Load the whole CSV once, then keep reading only the new lines (like `tail -f`)."""
    path = path or CSV_FILE
    parse = parse or parse_row
    target = rows if target is None else target
    lock = lock or rows_lock
    position = 0
    header = None
    while True:
        try:
            if not os.path.exists(path):
                time.sleep(2)
                continue
            if os.path.getsize(path) < position:   # file was deleted/rotated
                position, header = 0, None
                with lock:
                    target.clear()

            with open(path, newline='') as f:
                f.seek(position)
                chunk = f.read()
                # only consume complete lines; a half-written last line waits for next pass
                last_nl = chunk.rfind('\n')
                if last_nl == -1:
                    time.sleep(1)
                    continue
                complete = chunk[:last_nl + 1]
                position += len(complete.encode('utf-8'))

            lines = complete.splitlines()
            if header is None and lines:
                header = next(csv.reader([lines[0]]))
                lines = lines[1:]

            new_rows = []
            for values in csv.reader(lines):
                if not values or values[0] == 'timestamp':
                    continue
                parsed = parse(dict(zip(header, values)))
                if parsed:
                    new_rows.append(parsed)
            if new_rows:
                with lock:
                    target.extend(new_rows)
        except Exception as e:
            print(f"CSV follower error: {e}")
        time.sleep(1)


# --- Helpers ---

def rows_since(minutes):
    cutoff = datetime.now() - timedelta(minutes=minutes)
    with rows_lock:
        return [r for r in rows if r['ts'] >= cutoff]


def ml_since(minutes):
    cutoff = datetime.now() - timedelta(minutes=minutes)
    with ml_lock:
        return [r for r in ml_rows if r['ts'] >= cutoff]


def downsample(data, max_points=600):
    """Keep charts fast: average buckets so we send at most ~max_points points."""
    if len(data) <= max_points:
        return data
    step = math.ceil(len(data) / max_points)
    out = []
    for i in range(0, len(data), step):
        bucket = data[i:i + step]
        avg = {'ts': bucket[-1]['ts']}
        for key in ('avg_bpm', 'gas_raw', 'mpu_temp', 'accel_g'):
            avg[key] = sum(b[key] for b in bucket) / len(bucket)
        # keep the worst alert/peak in the bucket so short spikes are not hidden
        avg['accel_g'] = max(b['accel_g'] for b in bucket)
        avg['finger'] = max(b['finger'] for b in bucket)
        for key in ('bpm_alert_status', 'gas_status', 'temp_status', 'fall_status'):
            avg[key] = max(b[key] for b in bucket)
        out.append(avg)
    return out


def stats(values):
    if not values:
        return None
    return {'min': round(min(values), 1), 'max': round(max(values), 1),
            'avg': round(sum(values) / len(values), 1)}


def episodes(data, cond, key=None):
    """Group alert rows into episodes. Short dips (< MERGE_GAP_SEC) don't split an episode."""
    out, cur = [], None
    for r in data:
        if not cond(r):
            continue
        if cur and (r['ts'] - cur['end']).total_seconds() <= MERGE_GAP_SEC:
            cur['end'] = r['ts']
            if key:
                cur['peak'] = max(cur['peak'], r[key])
        else:
            cur = {'start': r['ts'], 'end': r['ts'], 'peak': r[key] if key else 0}
            out.append(cur)
    return out


def count_events(data, key, level=1):
    return len(episodes(data, lambda r: r[key] >= level))


def pct(data, cond):
    return round(100 * sum(1 for r in data if cond(r)) / len(data), 1) if data else 0


# --- Routes ---

@app.after_request
def no_cache(resp):
    """Always fetch the newest page and data, so an updated dashboard shows up after a normal reload."""
    if resp.mimetype in ('text/html', 'application/json'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/')
def index():
    return render_template('index.html', thresholds={
        'bpm_high': BPM_THRESHOLD_HIGH, 'gas_warn': GAS_THRESHOLD_WARN,
        'gas_danger': GAS_THRESHOLD_DANGER, 'temp_high': TEMP_THRESHOLD_HIGH,
        'temp_low': TEMP_THRESHOLD_LOW, 'impact_g': IMPACT_THRESHOLD_G})


@app.route('/api/latest')
def api_latest():
    with rows_lock:
        last = rows[-1] if rows else None
    if not last:
        return jsonify({'ok': False, 'reason': f'No data yet in {CSV_FILE}'})
    age = (datetime.now() - last['ts']).total_seconds()
    out = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in last.items() if k != 'ts'}
    out.update(ok=True, timestamp=last['ts'].strftime('%Y-%m-%d %H:%M:%S'),
               age_sec=round(age), online=age <= STALE_AFTER_SEC)
    with ml_lock:
        ml = ml_rows[-1] if ml_rows else None
    ml_ok = ml is not None and (datetime.now() - ml['ts']).total_seconds() <= STALE_AFTER_SEC
    out['ml_online'] = ml_ok
    out['version'] = DASHBOARD_VERSION
    if ml_ok and ml['fall_state'] in ('impact', 'fall_suspected', 'man_down'):
        # The 1 s accel snapshot misses the spike, so show the impact peak of this episode.
        peaks = [r['peak_g'] for r in ml_since(1) if r['peak_g'] is not None]
        out['ml_peak_g'] = max(peaks) if peaks else None
    if ml_ok:
        out.update(ml_fall_state=ml['fall_state'], ml_fall_reason=ml['fall_reason'],
                   ml_tilt_deg=ml['tilt_deg'], ml_still_s=ml['still_s'],
                   ml_risk_label=ml['risk_label'], ml_anomaly=ml['anomaly'])
    return jsonify(out)


@app.route('/api/workers')
def api_workers():
    """Latest ML result per worker, grouped into low / medium / high risk columns."""
    cutoff = datetime.now() - timedelta(seconds=WORKER_HIDE_AFTER_SEC)
    latest = {}
    with ml_lock:
        for r in reversed(ml_rows):
            if r['ts'] < cutoff:
                break
            latest.setdefault(r['worker_id'], r)
    peaks = {}
    for r in ml_since(1):
        if r['peak_g'] is not None:
            peaks[r['worker_id']] = max(peaks.get(r['worker_id'], 0), r['peak_g'])
    board = {'low': [], 'medium': [], 'high': []}
    for wid, r in sorted(latest.items()):
        fall = r['fall_state'] in ('fall_suspected', 'man_down')
        column = 'high' if fall else RISK_COLUMN.get(r['risk_label'], 'medium')
        age = (datetime.now() - r['ts']).total_seconds()
        board[column].append({
            'worker_id': wid, 'risk_label': r['risk_label'], 'fall_state': r['fall_state'],
            'fall_reason': r['fall_reason'], 'reason': 'fall / man down' if fall else f"model: {r['risk_label']}",
            'anomaly': r['anomaly'], 'tilt_deg': r['tilt_deg'], 'still_s': r['still_s'],
            'accel_g': max(r['accel_g'] or 0, peaks.get(wid, 0)) if r['fall_state'] == 'impact' else r['accel_g'],
            'gas': r['gas'], 'avg_bpm': r['avg_bpm'], 'finger': r['finger'], 'mpu_temp': r['mpu_temp'],
            'last_seen': r['ts'].strftime('%H:%M:%S'), 'age_sec': round(age), 'online': age <= STALE_AFTER_SEC})
    for col in board.values():  # falls first, then offline workers last
        col.sort(key=lambda w: (not w['fall_state'] in ('fall_suspected', 'man_down'), not w['online'], w['worker_id']))
    return jsonify(board)


@app.route('/api/history')
def api_history():
    minutes = max(1, min(int(request.args.get('minutes', 15)), 60 * 24))
    raw = rows_since(minutes)
    # Put the ESP32's impact peaks (from ml_live_log.csv) into the acceleration chart; the 1 s
    # snapshot in the main CSV almost never catches the ~0.1 s spike.
    peaks = {}
    for r in ml_since(minutes):
        if r['peak_g'] is not None:
            k = r['ts'].replace(microsecond=0)
            peaks[k] = max(peaks.get(k, 0), r['peak_g'])
    if peaks:
        raw = [dict(r, accel_g=max(r['accel_g'], max(peaks.get(r['ts'] + timedelta(seconds=d), 0) for d in (-1, 0, 1))))
               for r in raw]
    data = downsample(raw)
    return jsonify({
        't': [r['ts'].strftime('%Y-%m-%d %H:%M:%S') for r in data],
        'bpm': [round(r['avg_bpm'], 1) if r['finger'] else None for r in data],
        'gas': [round(r['gas_raw']) for r in data],
        'temp': [round(r['mpu_temp'], 2) for r in data],
        'accel_g': [round(r['accel_g'], 2) for r in data],
        'bpm_alert': [int(r['bpm_alert_status']) for r in data],
        'gas_status': [int(r['gas_status']) for r in data],
        'temp_status': [int(r['temp_status']) for r in data],
        'fall_status': [int(r['fall_status']) for r in data],
    })


@app.route('/api/stats')
def api_stats():
    minutes = max(1, min(int(request.args.get('minutes', 15)), 60 * 24))
    data = rows_since(minutes)
    ml = ml_since(minutes)
    with_finger = [r for r in data if r['finger'] and r['avg_bpm'] > 0]
    bpm = [r['avg_bpm'] for r in with_finger]

    zones = {'<60 (low)': 0, '60-80 (normal)': 0, '80-100 (elevated)': 0, '>100 (high)': 0}
    for b in bpm:
        if b < 60: zones['<60 (low)'] += 1
        elif b <= 80: zones['60-80 (normal)'] += 1
        elif b <= 100: zones['80-100 (elevated)'] += 1
        else: zones['>100 (high)'] += 1

    # activity level from how far acceleration strays from 1 g
    motion = [abs(r['accel_g'] - 1) for r in data]
    activity = {'Still': 0, 'Light': 0, 'Active': 0}
    for m in motion:
        if m < 0.05: activity['Still'] += 1
        elif m < 0.3: activity['Light'] += 1
        else: activity['Active'] += 1

    return jsonify({
        'samples': len(data),
        'bpm': stats(bpm), 'gas': stats([r['gas_raw'] for r in data]),
        'temp': stats([r['mpu_temp'] for r in data]),
        'accel_g': stats([r['accel_g'] for r in data]),
        'finger_pct': pct(data, lambda r: r['finger']),
        'alerts': {
            'Heart rate high': {'events': count_events(data, 'bpm_alert_status'),
                                'pct': pct(data, lambda r: r['bpm_alert_status'] >= 1)},
            'Gas warning+': {'events': count_events(data, 'gas_status'),
                             'pct': pct(data, lambda r: r['gas_status'] >= 1)},
            'Gas danger': {'events': count_events(data, 'gas_status', 2),
                           'pct': pct(data, lambda r: r['gas_status'] >= 2)},
            'Temperature': {'events': count_events(data, 'temp_status'),
                            'pct': pct(data, lambda r: r['temp_status'] >= 1)},
            'Fall suspected': {'events': len(episodes(ml, lambda r: r['fall_state'] == 'fall_suspected')),
                               'pct': pct(ml, lambda r: r['fall_state'] == 'fall_suspected')},
            'Man down': {'events': len(episodes(ml, lambda r: r['fall_state'] == 'man_down')),
                         'pct': pct(ml, lambda r: r['fall_state'] == 'man_down')},
        },
        'bpm_zones': zones,
        'activity': activity,
    })


@app.route('/api/events')
def api_events():
    """Last 50 alert episodes (start time, type, peak value)."""
    data = rows_since(60 * 24)
    checks = [
        ('Heart rate high', lambda r: r['bpm_alert_status'] >= 1, 'avg_bpm', 'BPM'),
        ('Gas danger', lambda r: r['gas_status'] >= 2, 'gas_raw', 'raw'),
        ('Gas warning', lambda r: r['gas_status'] == 1, 'gas_raw', 'raw'),
        ('Temperature high', lambda r: r['temp_status'] == 1, 'mpu_temp', '°C'),
        ('Temperature low', lambda r: r['temp_status'] == 2, 'mpu_temp', '°C'),
    ]
    events = []
    for name, cond, key, unit in checks:
        for e in episodes(data, cond, key):
            e.update(type=name, unit=unit)
            events.append(e)
    # Falls come from live_inference.py (ml_live_log.csv); peak = largest tilt from upright
    ml = [r for r in ml_since(60 * 24) if r['tilt_deg'] is not None]
    for name, state in (('MAN DOWN', 'man_down'), ('Fall suspected', 'fall_suspected')):
        for e in episodes(ml, lambda r, st=state: r['fall_state'] == st, 'tilt_deg'):
            e.update(type=name, unit='° tilt')
            events.append(e)
    events.sort(key=lambda e: e['start'], reverse=True)
    return jsonify([{
        'type': e['type'], 'start': e['start'].strftime('%Y-%m-%d %H:%M:%S'),
        'duration_sec': int((e['end'] - e['start']).total_seconds()) + 1,
        'peak': round(e['peak'], 1), 'unit': e['unit']} for e in events[:50]])


@app.route('/download.csv')
def download_csv():
    """Export the selected time range as CSV (or the whole log with ?minutes=0)."""
    minutes = int(request.args.get('minutes', 0))
    if minutes <= 0 and os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'rb') as f:
            content = f.read()
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['timestamp'] + NUMERIC_FIELDS + ['accel_g', 'fall_status'])
        for r in rows_since(minutes):
            w.writerow([r['ts'].strftime('%Y-%m-%d %H:%M:%S')] +
                       [r[f] for f in NUMERIC_FIELDS] + [round(r['accel_g'], 3), r['fall_status']])
        content = buf.getvalue()
    name = f"health_log_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(content, mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={name}'})


if __name__ == '__main__':
    print(f"Health Monitor dashboard {DASHBOARD_VERSION}")
    print(f"Reading data from: {CSV_FILE}")
    print(f"Reading fall state from: {ML_CSV} (written by live_inference.py)")
    threading.Thread(target=follow_csv, daemon=True).start()
    threading.Thread(target=follow_csv, args=(ML_CSV, parse_ml_row, ml_rows, ml_lock), daemon=True).start()
    print(f"Dashboard running: open http://<your-pi-ip>:{PORT} in a browser")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
