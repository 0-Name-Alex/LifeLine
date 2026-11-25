"""Raspberry Pi ML subscriber: READS existing ESP32 topic; PUBLISHES advisory predictions only."""
import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from paho.mqtt import client as mqtt
from predictor import RiskPredictor

FALL_ALERT_STATES = ('fall_suspected', 'man_down')
# Risk level sent to each jacket's LEDs/buzzer on pi/risk/<worker_id>. FALL is the HIGH level for a fall.
RISK_LEVEL = {'Normal': 'LOW', 'Warning': 'MEDIUM', 'High Risk': 'HIGH'}
LOG_FIELDS = ['timestamp_utc', 'worker_id', 'risk_label', 'anomaly', 'anomaly_score',
              'fall_state', 'fall_reason', 'tilt_deg', 'accel_g', 'still_s',
              'gas', 'avg_bpm', 'finger', 'mpu_temp']  # raw readings for the per-worker dashboard cards


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--broker', default=os.environ.get('MQTT_HOST', 'localhost'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('MQTT_PORT', '1883')))
    parser.add_argument('--input-topic', default='esp32/sensor_data')
    parser.add_argument('--output-topic', default='pi/ml_risk')
    parser.add_argument('--fall-topic', default='pi/fall_alert',
                        help='one message each time a worker enters/leaves a fall or man-down state')
    parser.add_argument('--esp32-fall-topic', default='esp32/fall_event',
                        help='immediate fall events from the ESP32 sketch in esp32/ (ignored if never sent)')
    parser.add_argument('--risk-topic-prefix', default='pi/risk/',
                        help='publishes LOW / MEDIUM / HIGH / FALL to <prefix><worker_id> for the jacket LEDs')
    parser.add_argument('--upright', default=None,
                        help='gravity direction when standing, e.g. "0,-1,0" (default 0,0,1 = z axis up)')
    parser.add_argument('--log-csv', type=Path, default=Path(__file__).resolve().parent / 'ml_live_log.csv',
                        help='also append every prediction to this CSV (default: ml_live_log.csv next to this script)')
    args = parser.parse_args()
    upright = tuple(float(v) for v in args.upright.split(',')) if args.upright else None
    predictor = RiskPredictor(upright=upright)
    fall_state = {}

    def log_row(result):
        if not args.log_csv:
            return
        if args.log_csv.exists():
            with args.log_csv.open(encoding='utf-8') as f:
                header = f.readline().strip().split(',')
            if header != LOG_FIELDS:  # log from an older version: keep it, start a new file
                old = args.log_csv.with_name(f'{args.log_csv.stem}_old_{datetime.now():%Y%m%d_%H%M%S}.csv')
                args.log_csv.rename(old)
                print('Old-format ML log moved to', old.name)
        new = not args.log_csv.exists()
        args.log_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.log_csv.open('a', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction='ignore')
            if new:
                w.writeheader()
            w.writerow(result)

    def publish_fall_change(client, worker, result):
        state = result['fall_state']
        before = fall_state.get(worker)
        fall_state[worker] = state
        if state == before or state == 'unknown':
            return
        if state in FALL_ALERT_STATES or before in FALL_ALERT_STATES:
            alert = {k: result.get(k) for k in LOG_FIELDS}
            alert['previous_state'] = before
            client.publish(args.fall_topic, json.dumps(alert), qos=1, retain=False)
            print('*** FALL STATE', worker, before, '->', state, '|', result['fall_reason'])

    def on_connect(client, userdata, flags, reason_code, properties=None):
        print('MQTT connected:', reason_code, 'subscribing:', args.input_topic, args.esp32_fall_topic)
        client.subscribe([(args.input_topic, 0), (args.esp32_fall_topic, 1)])

    def on_message(client, userdata, msg):
        try:
            packet = json.loads(msg.payload.decode('utf-8'))
            worker = str(packet.get('worker_id', 'worker_1'))
            now = datetime.now(timezone.utc)
            if msg.topic == args.esp32_fall_topic:
                # The ESP32 already decided; forward straight away, no model involved.
                alert = {'timestamp_utc': now.isoformat(), 'worker_id': worker,
                         'fall_state': 'fall_suspected', 'fall_reason': 'esp32_fall_event',
                         'previous_state': fall_state.get(worker), **packet}
                fall_state[worker] = 'fall_suspected'
                client.publish(args.fall_topic, json.dumps(alert), qos=1, retain=False)
                client.publish(args.risk_topic_prefix + worker, 'FALL', qos=1, retain=False)
                print('*** ESP32 FALL EVENT', worker, packet)
                return
            result = predictor.predict(packet, now=now.astimezone().replace(tzinfo=None))
            result['timestamp_utc'] = now.isoformat()
            result['source_topic'] = msg.topic
            for k in ('gas', 'avg_bpm', 'finger', 'mpu_temp'):
                result[k] = packet.get(k)
            client.publish(args.output_topic, json.dumps(result), qos=0, retain=False)
            level = 'FALL' if result['fall_state'] in FALL_ALERT_STATES else RISK_LEVEL.get(result['risk_label'], 'MEDIUM')
            client.publish(args.risk_topic_prefix + worker, level, qos=0, retain=False)
            publish_fall_change(client, worker, result)
            log_row(result)
            print(result['timestamp_utc'], worker, result['risk_label'],
                  'anomaly=', result['anomaly'], 'score=', result['anomaly_score'],
                  'fall=', result['fall_state'], 'tilt=', result['tilt_deg'])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            print('Rejected malformed/incomplete sensor packet:', exc)
        except Exception as exc:
            print('Inference error:', type(exc).__name__, str(exc))

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='SmartJacket_ML_Advisory')
    except AttributeError:
        # paho-mqtt 1.x (older Raspberry Pi OS apt package): same callbacks work, properties stays None.
        client = mqtt.Client(client_id='SmartJacket_ML_Advisory')
    username = os.environ.get('MQTT_USERNAME')
    if username:
        client.username_pw_set(username, os.environ.get('MQTT_PASSWORD'))
    client.on_connect = on_connect
    client.on_message = on_message
    print('Starting ML advisory subscriber; existing emergency alert controller stays unchanged.')
    client.connect(args.broker, args.port, keepalive=60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        client.disconnect()

if __name__ == '__main__':
    main()
