#!/usr/bin/env bash
# Pi Zero: start the iris camera client at boot and allow it to power the
# Pi Zero off after SUCCESS. Run from the camera-client folder:
#   sudo ./install_pizero.sh
# Join the Pi 5's Wi-Fi first:  sudo nmcli dev wifi connect <ssid> password <password>
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
DIR=$(cd "$(dirname "$0")" && pwd)
RUN_AS=${SUDO_USER:?run via sudo from your normal user}

apt-get install -y python3-opencv python3-numpy python3-picamera2

# Only `shutdown -h now` is allowed without a password.
echo "$RUN_AS ALL=(root) NOPASSWD: /usr/sbin/shutdown -h now" > /etc/sudoers.d/lifelink-iris-camera
chmod 440 /etc/sudoers.d/lifelink-iris-camera
visudo -cf /etc/sudoers.d/lifelink-iris-camera

cat > /etc/systemd/system/lifelink-iris-camera.service <<UNIT
[Unit]
Description=LifeLink iris camera client
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/camera_client.py --camera picamera2
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now lifelink-iris-camera.service
echo "Installed. Logs: journalctl -u lifelink-iris-camera -f"
