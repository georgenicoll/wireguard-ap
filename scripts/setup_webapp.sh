#!/usr/bin/env bash
# Sets up and runs the mnh-ap web UI (FastAPI + htmx) as a systemd service.
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

# Self-signed - there's no real hostname for the Pi to get a CA-signed cert
# for. Browsers will warn once; that's expected for a local device like
# this. Generated once and left alone on later runs, so it survives
# redeploys and the browser doesn't need re-trusting every time app.py
# changes.
CERT="${APP_DIR}/cert.pem"
KEY="${APP_DIR}/key.pem"
if [[ ! -f $CERT || ! -f $KEY ]]; then
  sudo -u "${PI_USER}" openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" -days 3650 \
    -subj "/CN=${AP_IP}" -addext "subjectAltName=IP:${AP_IP}"
fi

tee /etc/systemd/system/mnh-ap-webapp.service >/dev/null <<EOF
[Unit]
Description=mnh-ap web UI
After=network.target

[Service]
Type=simple
User=${PI_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
# Lets a non-root process bind port 443 (the default HTTPS port) without
# needing to run this whole service as root just for that. Deliberately
# *not* also narrowing CapabilityBoundingSet to just this capability - that
# restricts the whole process tree, not just this process, and broke sudo
# in the subprocesses app.py shells out to (/wireguard, /clients): sudo
# itself needs capabilities well beyond CAP_NET_BIND_SERVICE to change to
# root, and a narrowed bounding set denies those to it too.
AmbientCapabilities=CAP_NET_BIND_SERVICE
ExecStart=/usr/local/bin/uv run app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mnh-ap-webapp.service
systemctl restart mnh-ap-webapp.service
