# iot_esp32: Smart Jacket firmware

```
iot_esp32/
├── smart_jacket/              <- open this folder in the Arduino IDE
│   ├── smart_jacket.ino       final sketch (sensors + MQTT + LEDs + fall detection)
│   └── FallDetector.h         100 Hz fall detector (must stay next to the .ino)
└── tests/test_fall_detector.cpp   optional PC test: g++ -std=c++17 test_fall_detector.cpp && ./a.out
```

## Libraries (Arduino IDE > Tools > Manage Libraries)

| Library | Author | Used for |
|---|---|---|
| PubSubClient | Nick O'Leary | MQTT |
| Adafruit MPU6050 | Adafruit | accelerometer / gyro (also installs Adafruit Unified Sensor and Adafruit BusIO) |
| SparkFun MAX3010x Pulse and Proximity Sensor Library | SparkFun | MAX30102 heart rate (`MAX30105.h`, `heartRate.h`) |
| Adafruit NeoPixel | Adafruit | WS2812B risk LED |

Board: install **esp32 by Espressif Systems** in Boards Manager, then pick **ESP32 Dev Module**.
`WiFi.h` and `Wire.h` come with the board package.

## Before flashing, check

- `ssid` / `password`: your Wi-Fi.
- `WORKER_ID`: a different name for every jacket (e.g. `worker_1`, `worker_2`). The dashboard's
  risk board shows one card per WORKER_ID.
- `mqtt_server`: the Pi's IP (`hostname -I` on the Pi), e.g. `192.168.1.50`.
- Pins: gas sensor 34, I2C SDA 21 / SCL 22, WS2812B risk LED data 13, buzzer 23.
  `RISK_LED_BRIGHTNESS` (default 60 of 255) sets how bright it is.
  GPIO 25 / 26 / 27 are no longer used (the old alert LEDs were removed).
- `BUZZER_PASSIVE`: leave `false` for an active buzzer (beeps by itself on DC). Set `true` if yours
  only clicks.
- Mounting: the MPU6050 **z axis must point up** when the worker stands (accel_z about +9.8 in the
  Serial Monitor). If not, change `FallDetector fall(0, 0, 1);` (e.g. `(0, -1, 0)`) and start the Pi's
  `live_inference.py` with the same `--upright`. There is no calibration step.

## Risk LED (WS2812B) and buzzer (ML risk from the Pi)

The Pi's `live_inference.py` publishes `LOW`, `MEDIUM`, `HIGH` or `FALL` to `pi/risk/<WORKER_ID>`
every second. The sketch subscribes to its own WORKER_ID:

| Level | WS2812B | Buzzer |
|---|---|---|
| LOW | green | silent |
| MEDIUM | yellow | silent |
| HIGH | red | 0.2 s beep every second |
| FALL (fall detected on the jacket, or fall / man down from the Pi) | red blinking fast | fast beeping |
| No ML risk from the Pi (live_inference.py not running or not updated) | colour from the jacket's own thresholds (gas 2000/3000, BPM > 80, temp >= 38) | as above |
| NO LINK (Wi-Fi or MQTT not connected) | blue blinking slowly | silent |

Risk goes up immediately and only goes down after the lower level has held for 5 s, so it doesn't
flicker. A fall detected on the jacket itself always shows and beeps, even with no Wi-Fi.

### Wiring

```
WS2812B (single LED or small breakout):
  5V / VDD  --> ESP32 VIN (5 V from USB)        [3.3 V also works, a bit dimmer]
  GND       --> ESP32 GND
  DIN       --[330 Ω]--> GPIO 13
  100 µF to 1000 µF capacitor across 5V and GND, close to the LED (- leg to GND)

Buzzer through an NPN transistor (2N2222 / BC547 / S8050):
  GPIO 23 --[1 kΩ]--> transistor BASE
                      transistor EMITTER --> GND
                      transistor COLLECTOR --> buzzer (-)
                      buzzer (+) --> 5 V (VIN) for a 5 V buzzer, or 3.3 V for a 3.3 V one
```

- **3.3 V logic:** the ESP32 sends 3.3 V data, while a WS2812B powered from 5 V officially wants
  about 3.5 V. One LED on short wires almost always works anyway. If it flickers or shows wrong
  colours, power the LED from 3.3 V instead, or add a 74AHCT125 level shifter on the data line.
- Use DIN, not DOUT (the arrow on the board points away from DIN). Connect GND before power.
- A 3-pin buzzer module (VCC, GND, I/O) already has the transistor: wire I/O straight to GPIO 23.
  Some modules are "active low" (beep when I/O is LOW). If yours beeps all the time and stops during
  alarms, swap HIGH and LOW in `buzzer()`. Set `BUZZER_PASSIVE = true` if your buzzer only clicks.

## What it does

- Reads the MPU6050 at 100 Hz at **±16 g** (the default ±2 g cuts off fall impacts) and runs
  `FallDetector.h` on every reading. A fall is published to `esp32/fall_event` immediately, and the
  WS2812B blinks red with fast beeping until the worker is upright again.
- Every second publishes to `esp32/sensor_data`, keys in the same order as the Pi CSV:
  `accel_x, accel_y, accel_z, gas, avg_bpm, finger, mpu_temp`, then
  `worker_id, gyro_x, gyro_y, gyro_z, accel_peak_g, accel_min_g, gyro_peak_dps, fall`.
- Listens on `pi/risk/<WORKER_ID>` (`LOW`, `MEDIUM`, `HIGH`, `FALL`) for the WS2812B and buzzer.
- Heart rate: 6-beat rolling average, reset when no finger is on the sensor (`finger` = 0/1).
- Never blocks: fall detection keeps running while Wi-Fi or MQTT reconnects. If the MAX30102 is
  missing it keeps going with `avg_bpm` 0 and `finger` 0. If the MPU6050 is missing it keeps going
  without fall detection and says so at startup.

Serial Monitor at 115200 baud. At startup it prints `=== Smart Jacket iot_esp32 sketch ===`, an I2C
scan (MPU6050 = 0x68, MAX30102 = 0x57), OK / NOT FOUND for each sensor, and the Wi-Fi and MQTT
status. After that: every packet each second, a `STATUS` line every 5 s, and `FALL DETECTED`.
If you only see `Fall detection running`, that is the old example sketch from the ML zip, not this one.

**Not compiled for the ESP32 yet** (no ESP32 toolchain was available). It was syntax-checked with
stand-in headers and the fall logic passes the 8 PC tests. If the IDE shows an error, paste it back.
