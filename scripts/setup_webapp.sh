#!/usr/bin/env bash
# Sets up and runs the mnh-ap web UI (FastAPI + htmx) as a systemd service.
# Dependencies aren't installed separately - app.py declares them itself via
# PEP 723 inline script metadata, and "uv run app.py" resolves/caches an
# environment for them on the fly. Safe to re-run: restarts the service
# every time, so a changed webapp/ actually takes effect (uv itself notices
# a changed dependency block and re-resolves automatically).
#
# The service runs as its own unprivileged user (WEB_USER below), not as
# PI_USER, which owns the deployed scripts and the rest of the Pi's setup.
# Otherwise anything that got code running inside the web app would be
# PI_USER too - able to edit any script sudoers lets root run, and so
# become root. As WEB_USER it can only run, not change, the few scripts
# sudoers grants it (see the sudoers file written below).
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
ENV_FILE="$(dirname "$SCRIPT")/wireguard-ap.env"
[[ -f $ENV_FILE ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

WEB_USER="mnh-web"
APP_DIR="$(dirname "$SCRIPT")/webapp"
SCRIPTS_DIR="$(dirname "$SCRIPT")"
# Everything the web app itself writes or keeps secret (TLS key, session
# secret, uv's cache) - owned by WEB_USER, and outside PI_USER's home and
# the deployed webapp/ so a redeploy never touches it.
STATE_DIR="/var/lib/mnh-ap"

# Installed system-wide (not per-user) so it's on PATH for both this script
# and the systemd service below, regardless of which user runs each. Only
# fetched once - needs internet the first time (e.g. during initial home
# setup), but uv's own package cache then covers re-runs without it.
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# --- the service account -------------------------------------------------
id "$WEB_USER" &>/dev/null || useradd --system --no-create-home \
  --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$WEB_USER"
install -d -o "$WEB_USER" -g "$WEB_USER" -m 0700 "$STATE_DIR"

# WEB_USER has to be able to *reach* the deployed scripts and webapp/ under
# PI_USER's home (mode 0700) to run them - traverse only (x), for that one
# user, via an ACL. Not a chmod, which would open the whole home dir to
# every account. It still can't list the directory or read anything in it
# that isn't world-readable (the env file, keys and so on are 0600).
setfacl -m "u:${WEB_USER}:x" "/home/${PI_USER}"

# --- TLS certificate ------------------------------------------------------
# Self-signed - there's no real hostname for the Pi to get a CA-signed cert
# for. Browsers will warn once; that's expected for a local device like
# this. Generated once and left alone on later runs, so it survives
# redeploys and the browser doesn't need re-trusting every time app.py
# changes.
CERT="${STATE_DIR}/cert.pem"
KEY="${STATE_DIR}/key.pem"
OLD_CERT="${APP_DIR}/cert.pem"
OLD_KEY="${APP_DIR}/key.pem"
if [[ ! -f $CERT || ! -f $KEY ]]; then
  if [[ -f $OLD_CERT && -f $OLD_KEY ]]; then
    # Migrating from when the app ran as PI_USER and kept these in webapp/:
    # keep the same certificate, so browsers that already trust it aren't
    # asked again.
    install -o "$WEB_USER" -g "$WEB_USER" -m 0644 "$OLD_CERT" "$CERT"
    install -o "$WEB_USER" -g "$WEB_USER" -m 0600 "$OLD_KEY" "$KEY"
  else
    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout "$KEY" -out "$CERT" -days 3650 \
      -subj "/CN=${AP_IP}" -addext "subjectAltName=IP:${AP_IP}"
    chown "$WEB_USER:$WEB_USER" "$CERT" "$KEY"
    chmod 0644 "$CERT"
    chmod 0600 "$KEY"
  fi
fi
# The old copies are readable by PI_USER only, but there's no reason to keep
# a second copy of the private key (or of the session secret, which now
# lives in STATE_DIR too - a stale one uploaded from a dev checkout would
# otherwise be a signing key someone else might hold).
rm -f "$OLD_CERT" "$OLD_KEY" "${APP_DIR}/.session_secret"

# --- sudoers for the service account -------------------------------------
# Only the scripts the web app actually runs as root, and where the set of
# arguments is fixed, only those exact arguments (a sudoers "" means no
# arguments at all). uplink_wifi.sh takes a free-form SSID and password, so
# it can't be narrowed; it validates them itself. setup_ap.sh alone needs
# SETENV (it re-runs itself with sudo --preserve-env=SSH_CONNECTION), and
# tags carry on down a list, so it goes last. Written to a temp file and
# checked with visudo first: a bad sudoers file can lock out sudo entirely.
SUDOERS_FILE="/etc/sudoers.d/020-${WEB_USER}-wireguard-ap"
SUDOERS_TMP="$(mktemp)"
trap 'rm -f "$SUDOERS_TMP"' EXIT
cat >"$SUDOERS_TMP" <<EOF
${WEB_USER} ALL=(root) NOPASSWD: ${SCRIPTS_DIR}/view_wireguard_status.sh "", ${SCRIPTS_DIR}/view_currently_associated_clients.sh "", ${SCRIPTS_DIR}/shutdown_pi.sh "", ${SCRIPTS_DIR}/shutdown_pi.sh reboot, ${SCRIPTS_DIR}/diagnostics_sudo.sh --run-command *, ${SCRIPTS_DIR}/uplink_wifi.sh, SETENV: ${SCRIPTS_DIR}/setup_ap.sh dual, ${SCRIPTS_DIR}/setup_ap.sh uplink 5, ${SCRIPTS_DIR}/setup_ap.sh uplink 2.4
EOF
visudo -cf "$SUDOERS_TMP" >/dev/null
install -o root -g root -m 0440 "$SUDOERS_TMP" "$SUDOERS_FILE"

# --- the service ---------------------------------------------------------
tee /etc/systemd/system/mnh-ap-webapp.service >/dev/null <<EOF
[Unit]
Description=mnh-ap web UI
After=network.target

[Service]
Type=simple
User=${WEB_USER}
# journalctl (the Diagnostics page's "logs") only shows a user the whole
# journal if they're in this group.
SupplementaryGroups=systemd-journal
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=STATE_DIR=${STATE_DIR}
Environment=WEBAPP_CERT=${CERT}
Environment=WEBAPP_KEY=${KEY}
# uv (and Python) need somewhere writable that isn't the read-only webapp/.
Environment=HOME=${STATE_DIR}
Environment=UV_CACHE_DIR=${STATE_DIR}/uv-cache
Environment=PYTHONDONTWRITEBYTECODE=1
# Lets a non-root process bind port 443 (the default HTTPS port) without
# needing to run this whole service as root just for that. Deliberately
# *not* also narrowing CapabilityBoundingSet to just this capability - that
# restricts the whole process tree, not just this process, and broke sudo
# in the subprocesses app.py shells out to (/wireguard, /clients): sudo
# itself needs capabilities well beyond CAP_NET_BIND_SERVICE to change to
# root, and a narrowed bounding set denies those to it too. For the same
# reason NoNewPrivileges must stay off (sudo is setuid), and sandboxing
# such as ProtectSystem would also apply to the setup scripts sudo runs.
AmbientCapabilities=CAP_NET_BIND_SERVICE
ExecStart=/usr/local/bin/uv run app.py
Restart=on-failure
# uv's cache for this user is empty the first time, so it may need to
# download; don't burn through the restart limit if the network is slow.
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mnh-ap-webapp.service
systemctl restart mnh-ap-webapp.service
