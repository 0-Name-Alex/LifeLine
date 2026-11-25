import paho.mqtt.client as mqtt
import json
import requests
import time
import math
import csv
import os
import warnings
from datetime import datetime

# --- Changes made when packaging into iot_pi (everything else is your original logic) ---
# 1. GAS_THRESHOLD_WARN is now 2000 (it was 3000, the same as DANGER, so "Warning" could never happen).
# 2. CSV_FILE is now next to this script (or HEALTH_CSV if set), so it no longer depends on the
#    folder you start the script from. dashboard.py reads the same file.
# 3. The MQTT client works with both paho-mqtt 1.x and 2.x (Raspberry Pi OS ships either).
# 4. ThingSpeak fields now match the channel "Smart Safety Vest" (see THINGSPEAK FIELDS below),
#    fall detection goes to field 6 and a combined alert level to field 8.
#    The Write API key is read from the THINGSPEAK_API_KEY environment variable or the line below.
# 5. LED commands on pi/led_control were removed: the jacket no longer has the 3 alert LEDs.
#    The jacket's WS2812B + buzzer get their level from live_inference.py on pi/risk/<worker_id>.

# --- Configuration (EDIT THESE VALUES) ---
MQTT_BROKER = "localhost"  # "localhost" since script runs on the Pi
MQTT_PORT = 1883
MQTT_TOPIC_SUBSCRIBE = "esp32/sensor_data"

# Your channel's Write API Key. Generate a new one (ThingSpeak > API Keys > Generate New Write API Key)
# and paste it here, or set the THINGSPEAK_API_KEY environment variable instead.
THINGSPEAK_API_KEY = os.environ.get("THINGSPEAK_API_KEY", "PASTE_NEW_WRITE_API_KEY_HERE")
THINGSPEAK_URL = "https://api.thingspeak.com/update"
THINGSPEAK_INTERVAL_SEC = 15 # ThingSpeak free plan rate limit (1 update every 15s)

# --- NEW: CSV Logging Configuration ---
CSV_FILE = os.environ.get('HEALTH_CSV', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'health_monitor_log.csv'))
CSV_HEADER = [
    'timestamp',
    'accel_x', 'accel_y', 'accel_z',
    'gas_raw', 'avg_bpm', 'finger', 'mpu_temp',
    'bpm_alert_status', 'gas_status', 'temp_status'
]

# --- THINGSPEAK FIELDS (channel 3124255) ---
# field1 Temperature        = mpu_temp (MPU6050 chip temperature)
# field2 Humidity           = not sent (no humidity sensor)  -> untick it in Channel Settings
# field3 Gas Level          = gas raw ADC 0-4095 (not ppm)   -> rename it "Gas Level (raw)"
# field4 Heart Rate (BPM)   = avg_bpm when a finger is on the sensor, else 0
# field5 SpO2 (%)           = not sent (the ESP32 code does not compute SpO2) -> untick it
# field6 Fall Detection     = 0 ok, 1 impact, 2 fall suspected, 3 man down
#                             (worst state since the last upload, so a short impact is not missed)
# field7 Body Temperature   = not sent (no body temperature sensor) -> untick it
# field8 Alert Status       = 0 normal, 1 warning, 2 danger (highest of heart rate, gas, temp, fall)

# --- Fall state from the ML / fall detection service (live_inference.py) and the ESP32 ---
MQTT_TOPIC_ML = "pi/ml_risk"            # live_inference.py result for every packet (has fall_state)
MQTT_TOPIC_ESP_FALL = "esp32/fall_event"  # sent by the ESP32 the moment it detects a fall
FALL_CODES = {'ok': 0, 'recovered': 0, 'unknown': 0, 'impact': 1, 'fall_suspected': 2, 'man_down': 3}
fall_code_now = 0          # latest fall state
fall_code_worst = 0        # worst fall state since the last ThingSpeak upload

# --- Global variable for ThingSpeak rate limiting ---
last_thingspeak_upload = 0

# --- Processing Thresholds (Calibrate these!) ---
# Heart Rate Alert (triggers if avg BPM is > 80)
BPM_THRESHOLD_HIGH = 80

# Gas Sensor: Raw ADC value (0-4095). Higher = more gas.
# These are placeholders! You MUST calibrate this with your sensor.
GAS_THRESHOLD_DANGER = 3000  # e.g., "Danger" status (code 2)
GAS_THRESHOLD_WARN = 2000    # e.g., "Warning" status (code 1) - was 3000, same as DANGER, so it never fired

# Body Temperature (Fever threshold)
TEMP_THRESHOLD_HIGH = 38.0  # Alert if temp is >= 38°C (Fever)
TEMP_THRESHOLD_LOW = 28.0   # Alert if temp is <= 28°C (Hypothermia)


# --- MQTT Callback Functions ---

def on_connect(client, userdata, flags, rc):
    """Called when the client connects to the broker."""
    # --- THIS BLOCK WAS MISSING ---
    if rc == 0:
        print(f"Connected to MQTT Broker at {MQTT_BROKER}")
        client.subscribe([(MQTT_TOPIC_SUBSCRIBE, 0), (MQTT_TOPIC_ML, 0), (MQTT_TOPIC_ESP_FALL, 1)])
        print(f"Subscribed to topics: {MQTT_TOPIC_SUBSCRIBE}, {MQTT_TOPIC_ML}, {MQTT_TOPIC_ESP_FALL}")
    # --- END OF MISSING BLOCK ---
    else:
        print(f"Failed to connect, return code {rc}")

def on_message(client, userdata, msg):
    """
    Called every time a message is received on the subscribed topic.
    This is the core processing logic.
    """
    # Make sure we can modify the global variable
    global last_thingspeak_upload, fall_code_now, fall_code_worst

    # Fall state messages only update the fall code; the sensor packets do the rest.
    if msg.topic in (MQTT_TOPIC_ML, MQTT_TOPIC_ESP_FALL):
        try:
            info = json.loads(msg.payload.decode('utf-8'))
            if msg.topic == MQTT_TOPIC_ESP_FALL:
                code = FALL_CODES['fall_suspected']
                print(">>> INSIGHT: ESP32 detected a FALL!")
            else:
                code = FALL_CODES.get(info.get('fall_state'), 0)
            fall_code_now = code
            fall_code_worst = max(fall_code_worst, code)
        except (ValueError, AttributeError):
            pass
        return

    try:
        # 1. Decode the JSON payload from the ESP32
        payload = msg.payload.decode('utf-8')
        data = json.loads(payload)

        print(f"Received data: {data}")

        # 2. Extract data from the JSON object
        # Use .get() to avoid errors if a key is missing
        ax = data.get('accel_x', 0)
        ay = data.get('accel_y', 0)
        az = data.get('accel_z', 0)
        gas_raw = data.get('gas', 0)
        avg_bpm = data.get('avg_bpm', 0)
        finger = data.get('finger', 0)
        temp_c = data.get('mpu_temp', 0)

        # 3. Process the data to get insights

        # --- MODIFIED: Heart Rate Alert (Status: 0=Normal, 1=High) ---
        bpm_alert_status = 0
        if finger == 1 and avg_bpm > BPM_THRESHOLD_HIGH:
            bpm_alert_status = 1
            print(f">>> INSIGHT: High Heart Rate Alert! (BPM: {avg_bpm})")

        # Gas Status (Status: 0=Good, 1=Warn, 2=Danger)
        if gas_raw >= GAS_THRESHOLD_DANGER:
            gas_status = 2
            print(">>> INSIGHT: Gas Alert! (DANGER)")
        elif gas_raw >= GAS_THRESHOLD_WARN:
            gas_status = 1
            print(">>> INSIGHT: Gas Alert (Warning)")
        else:
            gas_status = 0

        # Temp Status (Status: 0=Normal, 1=High, 2=Low)
        if temp_c >= TEMP_THRESHOLD_HIGH:
             temp_status = 1
             print(">>> INSIGHT: High Body Temp Alert! (Fever)")
        elif temp_c <= TEMP_THRESHOLD_LOW and temp_c != 0: # != 0 to avoid false alert on startup
             temp_status = 2
             print(">>> INSIGHT: Low Body Temp Alert! (Hypothermia)")
        else:
            temp_status = 0

        # BPM Status
        # --- MODIFIED: Send avg_bpm if finger is detected, otherwise send 0 ---
        if finger == 1:
             bpm_to_send = avg_bpm
        else:
             bpm_to_send = 0

        # --- NEW: 3.5. Log raw data and processed status to CSV ---
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            data_row = [
                timestamp,
                ax, ay, az,
                gas_raw, avg_bpm, finger, temp_c,
                bpm_alert_status, gas_status, temp_status
            ]

            with open(CSV_FILE, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(data_row)

        except Exception as e:
            print(f"Error writing to CSV: {e}")

        # 4. (LED commands removed: the jacket no longer has the 3 alert LEDs)

        # 5. Upload processed data to ThingSpeak (This is now rate-limited)
        current_time = time.time()
        if (current_time - last_thingspeak_upload) > THINGSPEAK_INTERVAL_SEC:
            print("Attempting to upload to ThingSpeak...")

            # Combined alert level: 0 normal, 1 warning, 2 danger
            fall_upload = fall_code_worst
            fall_level = 2 if fall_upload >= 2 else fall_upload   # impact = warning, fall/man down = danger
            alert_level = max(bpm_alert_status, gas_status, 1 if temp_status else 0, fall_level)

            # These fields match your ThingSpeak channel setup (see THINGSPEAK FIELDS at the top)
            ts_payload = {
                'api_key': THINGSPEAK_API_KEY,
                'field1': temp_c,           # Field 1: Temperature (MPU6050 chip, °C)
                'field3': gas_raw,          # Field 3: Gas Level (raw ADC)
                'field4': bpm_to_send,      # Field 4: Heart Rate (BPM)
                'field6': fall_upload,      # Field 6: Fall Detection (0 ok, 1 impact, 2 fall, 3 man down)
                'field8': alert_level       # Field 8: Alert Status (0 normal, 1 warning, 2 danger)
            }

            try:
                # Send the data to ThingSpeak
                r = requests.post(THINGSPEAK_URL, data=ts_payload, timeout=5)

                if r.status_code == 200 and r.text != '0':
                    last_thingspeak_upload = current_time # Update timestamp *only* on success
                    fall_code_worst = fall_code_now       # start collecting the worst state again
                    print(f"Successfully uploaded to ThingSpeak (Entry ID: {r.text})")
                else:
                    # This catches ThingSpeak errors (like rate limit or wrong API key)
                    print(f"Error uploading to ThingSpeak. Status: {r.status_code}, Response: {r.text}")

            except requests.exceptions.RequestException as e:
                # This catches network errors (e.g., Pi is offline)
                print(f"ThingSpeak HTTP request failed: {e}")
        else:
            print("ThingSpeak upload skipped (rate limit).")

    except json.JSONDecodeError:
        print(f"Error: Received non-JSON message: {msg.payload.decode('utf-8')}")
    except Exception as e:
        print(f"An error occurred in on_message: {e}")

# --- Main Program ---

# --- NEW: Setup CSV file with header if it doesn't exist ---
if not os.path.exists(CSV_FILE):
    try:
        with open(CSV_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
    except Exception as e:
        print(f"CRITICAL: Could not create CSV file '{CSV_FILE}'. Error: {e}")
        # Depending on requirements, you might want to exit here
        # import sys
        # sys.exit()

def make_client():
    """paho-mqtt 2.x needs the callback API version; 1.x does not have it."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)  # VERSION1 keeps your on_connect(rc) unchanged
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="Pi_Processor_Script")
    except AttributeError:
        return mqtt.Client(client_id="Pi_Processor_Script")

def main():
    """Main function to set up and run the MQTT client."""

    # Check if dependencies are installed
    print("Checking dependencies (paho-mqtt, requests)...")
    print("If script fails, run: sudo apt install python3-paho-mqtt python3-requests")
    print(f"Logging to CSV: {CSV_FILE}")
    if THINGSPEAK_API_KEY.startswith("PASTE_"):
        print("WARNING: no ThingSpeak Write API key set. Paste it into THINGSPEAK_API_KEY in processor.py "
              "(or set the THINGSPEAK_API_KEY environment variable). Uploads will fail until then.")

    client = make_client()
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except ConnectionRefusedError:
        print("Error: Connection refused.")
        print("Please ensure the MQTT broker (e.g., Mosquitto) is running on your Pi.")
        print("You can start it with: sudo systemctl start mosquitto")
        return
    except Exception as e:
        print(f"Could not connect to MQTT broker: {e}")
        return

    # Loop forever to listen for messages
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nDisconnecting from MQTT broker...")
        client.disconnect()
        print("Script terminated.")

if __name__ == "__main__":
    main()
