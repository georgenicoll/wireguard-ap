#!/usr/bin/env bash
# Powers off the Pi, or reboots it if called with "reboot" as the first
# argument. Self-elevates rather than being sudo-prefixed at the call
# site, matching the other scripts, so it can also be triggered
# non-interactively by the web UI (no TTY for a password prompt).
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

if [[ "${1:-}" == "reboot" ]]; then
  systemctl reboot
else
  systemctl poweroff
fi
