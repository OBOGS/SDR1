#!/usr/bin/env bash
# Installs a systemd service so ~/SDR1/radio.py starts at boot.
# Run as your normal user (not root):  bash ~/SDR1/install_service.sh
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo. It calls sudo where needed."
    exit 1
fi

USER_NAME="$(id -un)"
HOME_DIR="$HOME"
APP_DIR="$HOME_DIR/SDR1"
PYTHON="$HOME_DIR/oled-env/bin/python"
SERVICE="sdr1-radio"

[ -f "$APP_DIR/radio.py" ] || { echo "Missing $APP_DIR/radio.py"; exit 1; }
[ -x "$PYTHON" ] || { echo "Missing venv python at $PYTHON"; exit 1; }

sudo tee /etc/systemd/system/${SERVICE}.service > /dev/null <<EOF
[Unit]
Description=SDR1 pocket radio (buttons + OLED + rtl_fm)
After=multi-user.target sound.target
Wants=sound.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
Environment=HOME=${HOME_DIR}
ExecStart=${PYTHON} -u ${APP_DIR}/radio.py
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=10
SupplementaryGroups=gpio i2c audio plugdev

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE}.service
sudo systemctl restart ${SERVICE}.service

echo
echo "Installed and started. Useful commands:"
echo "  systemctl status ${SERVICE}"
echo "  journalctl -u ${SERVICE} -f"
echo "  sudo systemctl stop ${SERVICE}      # before running radio.py by hand"
echo "  sudo systemctl disable ${SERVICE}   # stop it starting at boot"
