# iot_pi: everything that runs on the Raspberry Pi

```
ESP32 --esp32/sensor_data (1 s)--> processor.py --> health_monitor_log.csv --> dashboard.py --> browser :5000
      --esp32/fall_event (instant)-->    |   \--> ThingSpeak (every 15 s)
                              live_inference.py --> pi/ml_risk, pi/fall_alert, ml_live_log.csv
                                                \--pi/risk/<worker_id>--> ESP32 WS2812B + buzzer
```

Three programs run at the same time. All of them read and write files inside this folder,
so it works from wherever you copy it (for example `/home/pi/iot_pi`).

| File | What it does |
|---|---|
| `processor.py` | Your processor script: MQTT in, alerts, CSV log, ThingSpeak |
| `dashboard.py`, `templates/`, `static/` | Web dashboard on port 5000 (reads `health_monitor_log.csv`, and the fall state from `ml_live_log.csv`) |
| `live_inference.py` | ML risk + anomaly + fall / man-down detection, writes `ml_live_log.csv` |
| `predictor.py`, `features.py`, `fall_detector.py`, `models/` | Used by `live_inference.py` |
| `train_models.py`, `training_data/` | Retrains the models on the Pi if they don't load (install.sh does this) |
| `check_models.py` | Checks the models load with the Pi's scikit-learn |
| `watch_predictions.py` | Optional: prints the ML results in a terminal |
| `install.sh` | One-time setup (apt packages, MQTT broker, models) |
| `run_all.sh` | Starts all 3 programs, Ctrl+C stops them |
| `install_services.sh`, `systemd/` | Optional: start all 3 automatically on boot |
| `requirements.txt` | Package list (for reference / virtual environments) |

Files created while running: `health_monitor_log.csv`, `ml_live_log.csv`, `logs/`, `results/`.

## Changes from the separate versions

- `processor.py`: `GAS_THRESHOLD_WARN` is now **2000** (it was 3000, the same as danger, so "Warning"
  could never happen). The CSV is always written next to the script. Works with paho-mqtt 1.x and 2.x.
  Everything else is your original logic.
- `live_inference.py`: writes `ml_live_log.csv` by default and also works with paho-mqtt 1.x.
- `train_models.py`: reads its data from `training_data/`.
- `dashboard.py` + `templates/index.html`: the Motion card and the red banner show the fall state from
  `live_inference.py` (`ml_live_log.csv`): Moving / Still, Impact, FALL SUSPECTED, MAN DOWN, Recovered.
  If `live_inference.py` isn't running, the card says "Fall detection offline". Fall and man-down
  episodes also appear in the alert log and the alert counts.
- Workers by risk board (dashboard v4): three columns, Low / Medium / High, one card per `worker_id`
  from `ml_live_log.csv` (model Normal = Low, Warning = Medium, High Risk = High; a suspected fall or
  man-down always goes to High). `live_inference.py` now also logs gas, avg_bpm, finger and mpu_temp
  per worker; an older `ml_live_log.csv` is renamed to `ml_live_log_old_<time>.csv` automatically.
  Give every jacket its own `WORKER_ID` in the ESP32 sketch. The top tiles and graphs come from
  `processor.py`, which doesn't separate workers, so with several jackets they mix them.

## Step by step

### 1. Copy the folder to the Pi

From your laptop (in the folder that contains `iot_pi.zip`):

```bash
scp iot_pi.zip pi@10.142.95.249:~/
```

Then on the Pi:

```bash
cd ~
unzip iot_pi.zip
cd iot_pi
```

### 2. Install (one time)

```bash
bash install.sh
```

This installs the packages with apt, makes sure Mosquitto accepts the ESP32 from the network
(only if you haven't configured a listener yourself), and checks the ML models. The models in
`models/` were saved with scikit-learn 1.8, which is newer than Raspberry Pi OS has, so on most Pis
`install.sh` retrains them from `training_data/` (a few minutes on a Pi 3B+). That is expected.

### 3. Stop the old copies

If the old processor script or the old dashboard is still running (from `~/health-dashboard` or
elsewhere), stop it first with Ctrl+C in its terminal. Otherwise two programs answer the ESP32 and
port 5000 is already taken.

### 4. Run

```bash
./run_all.sh
```

You'll see the processor output live. The other two log to `logs/ml.log` and `logs/dashboard.log`
(`tail -f logs/ml.log` in a second terminal). Then open the dashboard from any phone or laptop on the
same Wi-Fi:

```
http://10.142.95.249:5000
```

(`run_all.sh` prints the right address if the Pi's IP has changed. If it has, update `mqtt_server`
in the ESP32 sketch too.)

### 5. Optional: start automatically on every boot

```bash
sudo ./install_services.sh
```

Logs: `journalctl -u iot-processor -f` (also `iot-ml`, `iot-dashboard`).
Stop/undo: `sudo systemctl disable --now iot-processor iot-ml iot-dashboard`.
Don't use `run_all.sh` at the same time as the services.

## ThingSpeak (channel 3124255)

Paste your Write API key into `THINGSPEAK_API_KEY` near the top of `processor.py` (or set the
`THINGSPEAK_API_KEY` environment variable). Uploads happen every 15 s:

| Field | Channel name | What is sent |
|---|---|---|
| 1 | Temperature | MPU6050 chip temperature (°C) |
| 2 | Humidity | nothing (no sensor): untick it |
| 3 | Gas Level | raw ADC 0-4095, not ppm: rename it "Gas Level (raw)" |
| 4 | Heart Rate (BPM) | average BPM, 0 when no finger |
| 5 | SpO2 (%) | nothing (not computed): untick it |
| 6 | Fall Detection | 0 ok, 1 impact, 2 fall suspected, 3 man down (worst since last upload) |
| 7 | Body Temperature (°C) | nothing (no body temperature sensor): untick it |
| 8 | Alert Status | 0 normal, 1 warning, 2 danger (highest of heart rate, gas, temperature, fall) |

The fall state comes from `live_inference.py` (`pi/ml_risk`) and the ESP32 (`esp32/fall_event`),
so keep `live_inference.py` running for field 6.

## MQTT topics and JSON keys (must match the ESP32)

| Topic | Direction | Content |
|---|---|---|
| `esp32/sensor_data` | ESP32 to Pi, every 1 s | `accel_x, accel_y, accel_z, gas, avg_bpm, finger, mpu_temp` (+ `worker_id, gyro_x/y/z, accel_peak_g, accel_min_g, gyro_peak_dps, fall`) |
| `esp32/fall_event` | ESP32 to Pi, instantly | `worker_id, event, impact_g, tilt_deg, fall_count` |
| `pi/ml_risk` | Pi | ML result for every packet |
| `pi/fall_alert` | Pi | Only when a fall / man-down starts or ends |
| `pi/risk/<worker_id>` | Pi to ESP32, every second | `LOW`, `MEDIUM`, `HIGH` or `FALL` (WS2812B + buzzer) |

Check what's arriving: `mosquitto_sub -h localhost -t 'esp32/#' -v` and
`mosquitto_sub -h localhost -t 'pi/#' -v`.

## Troubleshooting

- **Dashboard won't open from the laptop:** run `curl http://localhost:5000` on the Pi. If that works,
  it's the network (same Wi-Fi? `sudo ufw allow 5000` if ufw is installed). If not, look in `logs/dashboard.log`.
- **No data on the dashboard:** check `mosquitto_sub -h localhost -t 'esp32/#' -v`. If nothing arrives,
  the ESP32 can't reach the broker (Pi IP, Wi-Fi, or Mosquitto not listening on the network).
- **`Models do not load`:** run `python3 train_models.py`, then `python3 check_models.py`.
- **Upright direction:** the fall code assumes the MPU6050 z axis points up when the worker stands
  (accel_z about +9.8). If yours is mounted differently, start `live_inference.py` with e.g.
  `--upright 0,-1,0` and change `FallDetector fall(0, 0, 1)` in the ESP32 sketch the same way.
- `mpu_temp` is the MPU6050 chip temperature, not body temperature, so the fever/hypothermia alerts
  only become meaningful with a real body temperature sensor.
- **`anomaly=True` on almost every line:** usually the gas sensor reading 0. The anomaly model was
  trained on gas values around 1,800 to 2,000 and has never seen 0, so every reading looks unusual.
  Fix the gas sensor wiring first. Don't retrain on data where gas is 0.
- The ML risk output is advisory only. ThingSpeak alerts come from `processor.py`'s rules.
