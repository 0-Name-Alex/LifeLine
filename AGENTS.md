# LifeLine — AI Agent Guide

End-to-end wearable IoT safety & biometric monitoring platform (**ESP32 Smart Safety Jacket + Raspberry Pi Edge AI + Flask/Chart.js Live Dashboard**).

## 1. Repository Layout & Subsystems
- **[`iot_esp32/`](iot_esp32/)**: Arduino/ESP32 Dev Module firmware.
  - [`smart_jacket/smart_jacket.ino`](iot_esp32/smart_jacket/smart_jacket.ino): Non-blocking loop polling MAX30102 (heart rate), MPU6050 (`±16 g` accel/gyro @ 100 Hz), MQ gas sensor (`GPIO 34`), driving a WS2812B LED (`GPIO 13`) and buzzer (`GPIO 23`). I2C is `SDA 21` / `SCL 22`.
  - [`smart_jacket/FallDetector.h`](iot_esp32/smart_jacket/FallDetector.h): Header-only 100 Hz fall detector (default upright axis `(0, 0, 1)` = `+9.8 m/s²` on `accel_z`). Must remain alongside `smart_jacket.ino`.
  - [`tests/test_fall_detector.cpp`](iot_esp32/tests/test_fall_detector.cpp): Standalone C++17 simulation harness covering 8 fall/non-fall test cases.
- **[`iot_pi/`](iot_pi/)**: Canonical Raspberry Pi Edge AI stack (all scripts read/write in the working directory).
  - [`processor.py`](iot_pi/processor.py): Subscribes to `esp32/sensor_data`, logs `health_monitor_log.csv`, triggers threshold alerts (`GAS_THRESHOLD_WARN = 2000`, danger `3000`), and uploads to ThingSpeak every 15 s.
  - [`features.py`](iot_pi/features.py), [`predictor.py`](iot_pi/predictor.py), [`fall_detector.py`](iot_pi/fall_detector.py), [`live_inference.py`](iot_pi/live_inference.py): Extracts 16 rolling-window features (`window_seconds: 20`), runs scikit-learn `risk_classifier.joblib` + `anomaly_detector.joblib` + man-down state machine, writes `ml_live_log.csv`, and publishes `LOW`/`MEDIUM`/`HIGH`/`FALL` to `pi/risk/<WORKER_ID>`.
  - [`dashboard.py`](iot_pi/dashboard.py) + `templates/` + `static/`: Full Flask web server on port `5000` (reads both `health_monitor_log.csv` and `ml_live_log.csv`).
  - [`train_models.py`](iot_pi/train_models.py) & [`check_models.py`](iot_pi/check_models.py): Verifies scikit-learn compatibility and retrains from `training_data/health_monitor_log.csv` if pickled `.joblib` versions mismatch.
- **[`health-dashboard/`](health-dashboard/)**: Standalone lightweight Flask dashboard (reads `health_monitor_log.csv` only); includes [`preview.png`](health-dashboard/preview.png).

## 2. Verification & Run Commands
- **ESP32 C++17 unit tests**:
  ```bash
  g++ -std=c++17 -Wall -Wextra -o test_fd iot_esp32/tests/test_fall_detector.cpp && ./test_fd
  ```
- **Python syntax check**:
  ```bash
  py -3.14 -m py_compile iot_pi/*.py health-dashboard/dashboard.py
  ```
- **Raspberry Pi setup / run**: `cd iot_pi && bash install.sh` then `bash run_all.sh` (or `bash install_services.sh` for systemd).

## 3. Critical Rules & Gotchas
- **NEVER commit real Wi-Fi credentials, ThingSpeak keys, MQTT passwords, or private LAN IPs.** Keep `"YOUR_WIFI_SSID"`, `"YOUR_WIFI_PASSWORD"`, `"YOUR_PI_IP_ADDRESS"` in `smart_jacket.ino`, and use `THINGSPEAK_API_KEY` / `MQTT_PASSWORD` env vars in Python scripts.
- **Ignore raw `.zip` backups**: Local backup `.zip` files in this folder are excluded by `.gitignore`; always edit extracted folders directly.
- **`mpu_temp` caveat** (see [`iot_pi/models/metadata.json`](iot_pi/models/metadata.json)): `mpu_temp` is MPU6050 die temperature, not ambient/skin temperature; `class_labels` `0/1/2` map to `Normal`/`Warning`/`High Risk`.
- **paho-mqtt compatibility**: Keep `processor.py` and `live_inference.py` compatible with both `paho-mqtt 1.x` and `2.x` (`CallbackAPIVersion` handling).
