#!/usr/bin/env bash
# Makes the 3 programs start on every boot (systemd). Run:  sudo ./install_services.sh
# Undo: sudo systemctl disable --now iot-processor iot-ml iot-dashboard
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-pi}"
for name in processor ml dashboard; do
    sed -e "s#__DIR__#$DIR#g" -e "s#__USER__#$RUN_USER#g" \
        "$DIR/systemd/iot-$name.service" > "/etc/systemd/system/iot-$name.service"
done
systemctl daemon-reload
systemctl enable --now iot-processor iot-ml iot-dashboard
systemctl --no-pager status iot-processor iot-ml iot-dashboard | grep -E 'iot-|Active:'
echo "Logs: journalctl -u iot-processor -f   (or iot-ml, iot-dashboard)"
