#!/usr/bin/env bash
# Pi 5: start the iris engine and the LifeLink backend at boot.
# Run from anywhere inside the repo, as the user that owns it:
#   sudo ./iris-module/pi5-setup/install_services.sh
# Prerequisites: backend/.venv and iris-module/engine/.venv set up,
# backend/.env filled in, models downloaded (see iris-module/README.md).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
RUN_AS=${SUDO_USER:?run via sudo from your normal user}

cat > /etc/systemd/system/lifelink-iris-engine.service <<UNIT
[Unit]
Description=LifeLink iris engine (LightIrisNet + VIS-IrisFormer)
After=network.target

[Service]
User=$RUN_AS
WorkingDirectory=$REPO/iris-module/engine
ExecStart=$REPO/iris-module/engine/.venv/bin/python iris_service.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/lifelink-backend.service <<UNIT
[Unit]
Description=LifeLink backend (GUI, API, Pi Zero /health + /capture)
After=network-online.target postgresql.service lifelink-iris-engine.service
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$REPO/backend
ExecStart=$REPO/backend/.venv/bin/python app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now lifelink-iris-engine.service lifelink-backend.service
systemctl --no-pager status lifelink-iris-engine.service lifelink-backend.service | grep -E "●|Active"
