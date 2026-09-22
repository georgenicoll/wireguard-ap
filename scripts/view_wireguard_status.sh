#!/usr/bin/env bash
# Prints wg0's peer status (endpoint, handshake, transfer, allowed IPs).
# "wg show" needs root to read interface state, and self-elevates rather
# than being sudo-prefixed at the call site, so it can also be run
# non-interactively by the web UI (no TTY for a password prompt) with a
# single sudoers entry - the same pattern as the setup_*.sh scripts.
# Private/preshared keys are never printed: "wg show" (unlike "wg show
# dump") hides them by default.
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

if ! ip link show wg0 &>/dev/null; then
  echo "wg0 is not set up (wireguard_client_name not configured, or the tunnel isn't up)."
  exit 0
fi

wg show wg0
