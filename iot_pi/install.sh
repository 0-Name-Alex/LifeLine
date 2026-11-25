#!/usr/bin/env bash
# One-time setup for iot_pi on Raspberry Pi OS. Run from anywhere:  bash install.sh
set -e
cd "$(dirname "$0")"

echo "== Installing packages (apt) =="
sudo apt update
sudo apt install -y mosquitto mosquitto-clients \
    python3-paho-mqtt python3-requests python3-flask \
    python3-numpy python3-pandas python3-sklearn python3-joblib

echo "== MQTT broker =="
# Mosquitto 2.x only listens on localhost by default, so the ESP32 could not connect.
# Open it to the local network, but only if you have not configured a listener yourself.
if ! grep -rqsE '^\s*listener' /etc/mosquitto/mosquitto.conf /etc/mosquitto/conf.d/; then
    printf 'listener 1883 0.0.0.0\nallow_anonymous true\n' | sudo tee /etc/mosquitto/conf.d/iot_lan.conf >/dev/null
    echo "Added /etc/mosquitto/conf.d/iot_lan.conf (port 1883 open on the LAN)"
fi
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto

echo "== ML models =="
if ! python3 check_models.py; then
    echo "Retraining the models on this Pi (takes a few minutes on a Pi 3B+)..."
    python3 train_models.py
    python3 check_models.py
fi

chmod +x run_all.sh install_services.sh
echo
echo "Done. Start everything with:  ./run_all.sh"
echo "Or start on every boot with:  sudo ./install_services.sh"
