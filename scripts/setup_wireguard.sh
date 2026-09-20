#!/usr/bin/env bash
# Brings up this Pi's WireGuard uplink to wireguard-router, from a client
# config staged alongside this script. Safe to re-run: always re-installs
# the config and restarts the unit, so a changed config actually takes
# effect rather than being ignored because the unit was already running.
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
CONF_STAGED="$(dirname "$SCRIPT")/.wg0.conf"
[[ -f $CONF_STAGED ]] || { echo "missing staged config: $CONF_STAGED" >&2; exit 1; }

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

install -o root -g root -m 0600 "$CONF_STAGED" /etc/wireguard/wg0.conf
rm -f "$CONF_STAGED"

systemctl enable wg-quick@wg0
systemctl restart wg-quick@wg0
