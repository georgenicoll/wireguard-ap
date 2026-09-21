#!/usr/bin/env bash
# Sets up and runs the mnet-ap web UI (FastAPI + htmx) as a systemd service.
# Dependencies aren't installed separately - app.py declares them itself via
# PEP 723 inline script metadata, and "uv run app.py" resolves/caches an
# environment for them on the fly. Safe to re-run: restarts the service
# every time, so a changed webapp/ actually takes effect (uv itself notices
# a changed dependency block and re-resolves automatically).
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
ENV_FILE="$(dirname "$SCRIPT")/wireguard-ap.env"
[[ -f $ENV_FILE ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

APP_DIR="$(dirname "$SCRIPT")/webapp"

# Installed system-wide (not per-user) so it's on PATH for both this script
# and the systemd service below, regardless of which user runs each. Only
# fetched once - needs internet the first time (e.g. during initial home
# setup), but uv's own package cache then covers re-runs without it.
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

tee /etc/systemd/system/mnet-ap-webapp.service >/dev/null <<EOF
[Unit]
Description=mnet-ap web UI
After=network.target

[Service]
Type=simple
User=${PI_USER}
WorkingDirectory=${APP_DIR}
ExecStart=/usr/local/bin/uv run app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mnet-ap-webapp.service
systemctl restart mnet-ap-webapp.service
