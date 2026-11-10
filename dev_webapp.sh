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
# Everything local dev generates (cert, key, session secret) lives here, not
# in webapp/: main.tf uploads all of webapp/ to the Pi, and none of this
# should ever go with it.
DEV_DIR="${SCRIPT_DIR}/.dev"
mkdir -p "$DEV_DIR"
chmod 700 "$DEV_DIR"
CERT="${DEV_DIR}/dev-cert.pem"
KEY="${DEV_DIR}/dev-key.pem"

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
export STATE_DIR="$DEV_DIR"
# No __pycache__ dirs inside webapp/ either (they'd be uploaded too).
export PYTHONDONTWRITEBYTECODE=1
export SSID="${SSID:-dev-ap}"
export PSK="${PSK:-devpassword}"
export WEBAPP_HOST="127.0.0.1"
export WEBAPP_PORT="8443"
export WEBAPP_CERT="$CERT"
export WEBAPP_KEY="$KEY"
export WEBAPP_RELOAD="1"
# The Metrics page is smweb's chart (the web server from the simple-metrics
# project) inside this app, and smweb reads a locally running simple-metrics:
# start that with run_local.sh in the simple-metrics repo (its default socket
# is the one below). smweb is started here if there's a build of it to use:
# SMWEB_BIN, or one in a simple-metrics checkout beside this repository.
COLLECTOR_SOCKET="${METRICS_SOCKET:-/tmp/simple-metrics.sock}"
export METRICS_WEB_SOCKET="${METRICS_WEB_SOCKET:-/tmp/smweb-dev.sock}"
SMWEB="${SMWEB_BIN:-}"
if [[ -z $SMWEB ]]; then
  for candidate in "${SCRIPT_DIR}/../simple-metrics/target/release/smweb" \
                   "${SCRIPT_DIR}/../simple-metrics/target/debug/smweb"; do
    [[ -x $candidate ]] && { SMWEB="$candidate"; break; }
  done
fi

echo "Login password: ${PSK} (override by setting PSK before running this script)"
SMWEB_PID=""
if [[ -z $SMWEB ]]; then
  echo "Metrics page: no smweb to use - build one in the simple-metrics repo (cargo build --bin smweb) or set SMWEB_BIN"
elif [[ ! -S $COLLECTOR_SOCKET ]]; then
  echo "Metrics page: nothing at ${COLLECTOR_SOCKET} - start simple-metrics with run_local.sh (in its repo) to see data"
else
  rm -f "$METRICS_WEB_SOCKET"
  # Mounted at /metrics, as the deployed service is (see setup_metrics.sh).
  "$SMWEB" --socket "$COLLECTOR_SOCKET" --unix "$METRICS_WEB_SOCKET" --unix-mode 600 \
    --base-path /metrics --title mnh-ap &
  SMWEB_PID=$!
  trap '[[ -n $SMWEB_PID ]] && kill "$SMWEB_PID" 2>/dev/null; rm -f "$METRICS_WEB_SOCKET"' EXIT
  echo "Metrics page: smweb (${SMWEB}) serving the simple-metrics at ${COLLECTOR_SOCKET}"
fi
echo "https://127.0.0.1:8443/ - browser will warn about the self-signed cert, that's expected"

cd "$APP_DIR"
# Not exec'd, so that smweb is stopped (by the trap above) when this ends.
uv run app.py
