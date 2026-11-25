# Health Monitor Dashboard (runs on the Pi)

A local web page that shows live values, alerts, graphs and stats from your ESP32 sensors.
It runs **next to** your existing processor script and reads the CSV that script writes
(`health_monitor_log.csv`), so you don't have to change that script at all.

```
ESP32 --MQTT--> processor script --> health_monitor_log.csv --> dashboard.py --> browser
                       |--> ThingSpeak, LEDs (unchanged)
```

## Files

```
health-dashboard/
├── dashboard.py              # Flask server
├── templates/index.html      # the dashboard page
└── static/chart.umd.min.js   # Chart.js 4.4.1 (served locally, works without internet)
```

Copy the whole folder to the Pi, ideally into the **same folder you run the processor script from**
(the CSV is written there).

## Run

```bash
pip install flask          # on newer Raspberry Pi OS: sudo apt install python3-flask
cd ~/health-dashboard
python3 dashboard.py
```

Then open `http://<pi-ip>:5000` from any phone or laptop on the same Wi-Fi.

If the CSV is in a different folder, tell the dashboard where it is:

```bash
HEALTH_CSV=/home/pi/health_monitor_log.csv python3 dashboard.py
```

## What's on the page

- **Live tiles** (refresh every 2 s): heart rate, gas, temperature, motion (g), finger on sensor, raw x/y/z.
  Each has a status badge; the tile turns red and a banner appears when an alert is active.
- **Online / offline dot**: goes red if no new reading for 10 s (ESP32 or processor script stopped).
- **Graphs** with the alert thresholds drawn as dashed lines: heart rate, gas, temperature, acceleration.
- **Time range** buttons: 5 min, 15 min, 1 h, 6 h, 24 h.
- **Analytics**: min / avg / max per parameter, number of alert episodes and % of time in alert,
  heart-rate zones bar chart, activity level (still / light / active).
- **Alert log**: every alert episode in the last 24 h with start time, duration and peak value.
- **CSV download**: the selected range, or the full log.
- **Fall / impact detection** (new): computed from the accelerometer. |a| > 2.5 g = possible fall.

## Start automatically on boot (optional)

Create `/etc/systemd/system/health-dashboard.service` (fix the paths and user):

```ini
[Unit]
Description=Health Monitor Dashboard
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/health-dashboard
Environment=HEALTH_CSV=/home/pi/health_monitor_log.csv
ExecStart=/usr/bin/python3 /home/pi/health-dashboard/dashboard.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now health-dashboard
```

You can do the same for the processor script so both start on boot.

## Settings to keep in sync

At the top of `dashboard.py`, `BPM_THRESHOLD_HIGH`, `GAS_THRESHOLD_WARN/DANGER` and
`TEMP_THRESHOLD_HIGH/LOW` are only used to draw the dashed lines. The alert badges come from
the CSV, so they always match what your processor script decided.
`GAS_THRESHOLD_WARN` is set to 2000 here; change it in your processor script too (it is 3000 there,
the same as danger, so "warning" can never happen).
