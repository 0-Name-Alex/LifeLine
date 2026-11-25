"""Console monitoring of the ML topic. Does not command any hardware."""
import argparse
import json
import os
from paho.mqtt import client as mqtt

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--broker', default=os.environ.get('MQTT_HOST', 'localhost'))
    p.add_argument('--port', type=int, default=int(os.environ.get('MQTT_PORT', '1883')))
    p.add_argument('--topic', default='pi/ml_risk')
    args = p.parse_args()
    def on_connect(client, userdata, flags, reason_code, properties=None):
        print('Listening:', args.topic, 'connection:', reason_code)
        client.subscribe(args.topic)
    def on_message(client, userdata, msg):
        try:
            d = json.loads(msg.payload)
            print(f"{d.get('timestamp_utc','')} {d.get('worker_id')} | "
                  f"risk={d.get('risk_label')} anomaly={d.get('anomaly')} "
                  f"anomaly_score={d.get('anomaly_score')} | "
                  f"fall={d.get('fall_state')} tilt={d.get('tilt_deg')} {d.get('fall_reason', '')}")
        except (ValueError, UnicodeDecodeError) as exc:
            print('Invalid JSON:', exc)
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='SmartJacket_ML_Watcher')
    except AttributeError:  # paho-mqtt 1.x
        c = mqtt.Client(client_id='SmartJacket_ML_Watcher')
    user = os.environ.get('MQTT_USERNAME')
    if user:
        c.username_pw_set(user, os.environ.get('MQTT_PASSWORD'))
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(args.broker, args.port, 60)
    try:
        c.loop_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        c.disconnect()
if __name__ == '__main__':
    main()
