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
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, Response

# --- Configuration (EDIT THESE VALUES) ---
# Must point to the same CSV your processor script writes.
# Your script uses a relative path, so this is the folder you START that script from.
CSV_FILE = os.environ.get("HEALTH_CSV", os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_monitor_log.csv"))

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


def follow_csv():
    """Load the whole CSV once, then keep reading only the new lines (like `tail -f`)."""
    position = 0
    header = None
    while True:
        try:
            if not os.path.exists(CSV_FILE):
                time.sleep(2)
                continue
            if os.path.getsize(CSV_FILE) < position:   # file was deleted/rotated
                position, header = 0, None
                with rows_lock:
                    rows.clear()

            with open(CSV_FILE, newline='') as f:
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
                parsed = parse_row(dict(zip(header, values)))
                if parsed:
                    new_rows.append(parsed)
            if new_rows:
                with rows_lock:
                    rows.extend(new_rows)
        except Exception as e:
            print(f"CSV follower error: {e}")
        time.sleep(1)


# --- Helpers ---

def rows_since(minutes):
    cutoff = datetime.now() - timedelta(minutes=minutes)
    with rows_lock:
        return [r for r in rows if r['ts'] >= cutoff]


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
    return jsonify(out)


@app.route('/api/history')
def api_history():
    minutes = max(1, min(int(request.args.get('minutes', 15)), 60 * 24))
    data = downsample(rows_since(minutes))
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
            'Possible fall': {'events': count_events(data, 'fall_status', 2),
                              'pct': pct(data, lambda r: r['fall_status'] >= 2)},
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
        ('Possible fall / impact', lambda r: r['fall_status'] >= 2, 'accel_g', 'g'),
    ]
    events = []
    for name, cond, key, unit in checks:
        for e in episodes(data, cond, key):
            e.update(type=name, unit=unit)
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
    print(f"Reading data from: {CSV_FILE}")
    threading.Thread(target=follow_csv, daemon=True).start()
    print(f"Dashboard running: open http://<your-pi-ip>:{PORT} in a browser")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
