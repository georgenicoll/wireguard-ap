#!/usr/bin/env bash
# Runs the web UI locally with hot reload, so UI changes (templates,
# static/, app.py) can be iterated on without deploying to the Pi each
# time. Not for exercising the "Run setup" / status / shutdown buttons -
# those shell out to scripts/*.sh, which aren't at the path app.py expects
# them (flat alongside webapp/, as they are once uploaded to the Pi - see
# app.py's SCRIPTS_DIR comment) and self-elevate via sudo to touch real
# network interfaces, so they're deliberately left unrunnable here.
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
APP_DIR="${SCRIPT_DIR}/webapp"
CERT="${APP_DIR}/dev-cert.pem"
KEY="${APP_DIR}/dev-key.pem"

command -v uv >/dev/null || { echo "uv not found - see https://docs.astral.sh/uv/getting-started/installation/" >&2; exit 1; }

# Self-signed, for localhost only - separate from cert.pem/key.pem (the
# names setup_webapp.sh uses on the Pi) so this never gets confused with,
# or accidentally clobbers, a real deployment's cert.
if [[ ! -f $CERT || ! -f $KEY ]]; then
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" -days 3650 \
    -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
fi

export SCRIPTS_DIR="$SCRIPT_DIR/scripts"
export SSID="${SSID:-dev-ap}"
export PSK="${PSK:-devpassword}"
export WEBAPP_HOST="127.0.0.1"
export WEBAPP_PORT="8443"
export WEBAPP_CERT="$CERT"
export WEBAPP_KEY="$KEY"
export WEBAPP_RELOAD="1"

echo "Login password: ${PSK} (override by setting PSK before running this script)"
echo "https://127.0.0.1:8443/ - browser will warn about the self-signed cert, that's expected"

cd "$APP_DIR"
exec uv run app.py
