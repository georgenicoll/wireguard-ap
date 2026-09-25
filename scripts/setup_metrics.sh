#!/usr/bin/env bash
# Installs and runs the simple-metrics collector (github.com/georgenicoll/
# simple-metrics) as a systemd service. Safe to re-run: it replaces the
# binary and restarts the service every time, so a new release takes effect.
#
# The collector samples the Pi's CPU, memory, temperature and network use
# into memory and serves them on a Unix socket; the web app reads that
# socket. Two things keep it separate from the web app:
#
#  - It runs as its own unprivileged user (METRICS_USER), and needs no
#    privileges at all (everything it reads is world-readable), so the unit
#    below drops every capability and locks the rest of the system down.
#  - It is a separate service, so its history survives the web app being
#    restarted by a redeploy. (It is lost when the collector itself is
#    restarted or upgraded, and on a reboot: nothing is written to disk.)
#
# The binary is uploaded next to this script as ./simple-metrics (by
# main.tf's metrics_deploy) and installed to /usr/local/bin, root-owned, so
# METRICS_USER can run it but never replace it.
#
#   setup_metrics.sh --print-unit   prints the systemd unit it would install
#                                   and exits (no root needed) - for tests.
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
SCRIPT_DIR="$(dirname "$SCRIPT")"
ENV_FILE="${SCRIPT_DIR}/wireguard-ap.env"
BINARY_SRC="${SCRIPT_DIR}/simple-metrics"
[[ -f $ENV_FILE ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

METRICS_USER="mnh-metrics"
# The web app's own user and group (see setup_webapp.sh): the socket is
# owned by this group, so the web app can connect and nothing else can.
WEB_GROUP="mnh-web"
SERVICE="mnh-ap-metrics"
BINARY="/usr/local/bin/simple-metrics"
SOCKET_DIR_NAME="simple-metrics"                    # /run/<this>, made by systemd
SOCKET="/run/${SOCKET_DIR_NAME}/simple-metrics.sock"
INTERVAL="5s"
RETENTION="7d"

# The network interfaces to chart: the wired uplink, the two radios, the AP
# bridge and the tunnel. The names come from the env file, like the web app's
# own interface list, with repeats removed (the two radios can be the same
# interface in some modes).
interface_args() {
  local -A seen=()
  local name args=()
  for name in eth0 "$IF_24" "$IF_5" "$BR" wg0; do
    [[ $name =~ ^[A-Za-z0-9._-]{1,15}$ ]] || { echo "bad interface name: '$name'" >&2; exit 1; }
    [[ -n ${seen[$name]:-} ]] && continue
    seen[$name]=1
    args+=("--interface" "$name")
  done
  echo "${args[*]}"
}
# Computed here, at the top level, not inside the unit's heredoc: an `exit`
# in a command substitution only leaves that substitution, so a bad name
# would print its error and still install a unit with no interfaces at all.
INTERFACE_ARGS="$(interface_args)"

# The unit is written out in full here rather than shipped as a file so that
# it can be checked with --print-unit. Every line under "Hardening" costs the
# collector nothing (see above); the ones that would stop it working are left
# out: it needs to read /proc and /sys (so no ProcSubset=pid, no
# InaccessiblePaths), and to see the machine's real network interfaces (so no
# PrivateNetwork=).
render_unit() {
  cat <<EOF
[Unit]
Description=simple-metrics collector for mnh-ap
Documentation=https://github.com/georgenicoll/simple-metrics

[Service]
Type=simple
User=${METRICS_USER}
# The group that owns the socket. Setting it here (rather than adding the web
# app's user to a group of ours) leaves the web app's own service unchanged.
Group=${WEB_GROUP}
# systemd creates /run/${SOCKET_DIR_NAME} for the service, owned by the user
# and group above and closed to everyone else, and removes it on stop. So the
# socket can never be reached by any other account, even before the
# collector has set the socket's own permissions.
RuntimeDirectory=${SOCKET_DIR_NAME}
RuntimeDirectoryMode=0750
UMask=0007
ExecStart=${BINARY} --socket ${SOCKET} --interval ${INTERVAL} --retention ${RETENTION} ${INTERFACE_ARGS}
Restart=on-failure
RestartSec=5

# Memory: about 15 MB of history, and roughly that again while a full read
# is being copied out. A backstop against a fault, not a target.
MemoryMax=128M
TasksMax=64

# Hardening. The collector reads /proc and /sys and serves one Unix socket.
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
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
RestrictAddressFamilies=AF_UNIX
IPAddressDeny=any
RemoveIPC=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service

[Install]
WantedBy=multi-user.target
EOF
}

if [[ ${1:-} == --print-unit ]]; then
  render_unit
  exit 0
fi

[[ -f $BINARY_SRC ]] || { echo "missing binary: $BINARY_SRC (main.tf uploads it)" >&2; exit 1; }
[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

getent group "$WEB_GROUP" >/dev/null \
  || { echo "group $WEB_GROUP doesn't exist - run setup_webapp.sh first" >&2; exit 1; }

id "$METRICS_USER" &>/dev/null || useradd --system --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin "$METRICS_USER"

# Install next to the destination and check it runs on this machine *before*
# replacing the current binary: a wrong-architecture or damaged upload must
# not take a working collector down. The final mv is atomic, so the service
# never sees a half-written file.
install -o root -g root -m 0755 "$BINARY_SRC" "${BINARY}.new"
if ! "${BINARY}.new" --version >/dev/null; then
  rm -f "${BINARY}.new"
  echo "the uploaded simple-metrics does not run on this machine" >&2
  exit 1
fi
mv -f "${BINARY}.new" "$BINARY"

render_unit >"/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable "${SERVICE}.service"
systemctl restart "${SERVICE}.service"

# Wait for the socket, so a failed start is reported here, where the deploy
# is running, rather than found later.
for _ in $(seq 1 20); do
  [[ -S $SOCKET ]] && break
  sleep 0.5
done
if [[ ! -S $SOCKET ]]; then
  echo "the collector did not create $SOCKET:" >&2
  journalctl -u "${SERVICE}.service" -n 20 --no-pager >&2 || true
  exit 1
fi
echo "$("$BINARY" --version) is running, listening on $SOCKET"
