#!/usr/bin/env bash
# Serves the web UI's TLS certificate over plain HTTP, so a device can
# install it as trusted straight from the network: visiting
# http://<pi>/wireguard-ap.crt downloads it with the MIME type that makes
# Chrome hand it to Android's own certificate installer - no separate file
# transfer, and plain HTTP sidesteps the very certificate-trust problem
# installing it is meant to solve.
#
# Only the public certificate ever leaves setup_webapp.sh's state
# directory - never key.pem, which stays put, owned by mnh-web only. Safe
# to re-run: restarts the service every time, so a regenerated certificate
# (setup_webapp.sh only makes a new one if the old one is missing - see its
# own comment) is picked up.
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
SCRIPTS_DIR="$(dirname "$SCRIPT")"
ENV_FILE="${SCRIPTS_DIR}/wireguard-ap.env"
[[ -f $ENV_FILE ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

CERT_USER="mnh-cert"
# The source is setup_webapp.sh's own STATE_DIR/CERT - the same
# certificate, just also exported here in DER form. SERVE_DIR is
# deliberately its own top-level directory, not nested under
# /var/lib/mnh-ap: that one is 0700 or mnh-web, so mnh-cert couldn't
# traverse into any subdirectory of it no matter that subdirectory's own
# permissions.
CERT_PEM="/var/lib/mnh-ap/cert.pem"
SERVE_DIR="/var/lib/mnh-ap-cert-http"
CERT_DER="${SERVE_DIR}/wireguard-ap.crt"

[[ -f $CERT_PEM ]] || { echo "missing $CERT_PEM - run setup_webapp.sh first" >&2; exit 1; }

id "$CERT_USER" &>/dev/null || useradd --system --no-create-home \
  --home-dir "$SERVE_DIR" --shell /usr/sbin/nologin "$CERT_USER"

# CERT_USER has to be able to *reach* cert_http_server.py under PI_USER's
# home (mode 0700) to run it - traverse only (x), same as setup_webapp.sh
# grants WEB_USER for the same reason.
setfacl -m "u:${CERT_USER}:x" "/home/${PI_USER}"

# World-readable: it's a public certificate, same content anyone can already
# read off the TLS handshake on 443.
install -d -o root -g root -m 0755 "$SERVE_DIR"
openssl x509 -in "$CERT_PEM" -outform der -out "$CERT_DER"
chown root:root "$CERT_DER"
chmod 0644 "$CERT_DER"

tee /etc/systemd/system/mnh-ap-cert-http.service >/dev/null <<EOF
[Unit]
Description=mnh-ap certificate download server (plain HTTP, for installing it as trusted)
After=network.target

[Service]
Type=simple
User=${CERT_USER}
ExecStart=/usr/bin/python3 ${SCRIPTS_DIR}/cert_http_server.py ${CERT_DER} 0.0.0.0 80
Restart=on-failure
RestartSec=5

# Lets a non-root process bind port 80 without running as root. Unlike the
# web app's own service, this one shells out to nothing, so it gets the
# full hardening below (same pattern as the metrics collector's unit).
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
# Not "yes": cert_http_server.py itself lives under PI_USER's home, like
# every other deployed script (see setup_webapp.sh's own comment on why its
# service skips this too). "read-only" still keeps this process from
# writing anything there.
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictAddressFamilies=AF_INET AF_INET6
RemoveIPC=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mnh-ap-cert-http.service
systemctl restart mnh-ap-cert-http.service

echo "Certificate download: http://${AP_IP}/wireguard-ap.crt (or http://wireguard-ap/wireguard-ap.crt from a device on the AP)"
