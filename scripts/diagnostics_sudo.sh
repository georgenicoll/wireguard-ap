#!/usr/bin/env bash
# The diagnostic commands that need root. Same command syntax as
# diagnostics.sh (see its header for the --show-commands/--run-command
# protocol) - diagnostics.sh delegates to this script for both, and knows
# nothing about which commands live here. To add a root-only command, add
# it here (same "register" + cmd_<name> pattern), not in diagnostics.sh.
#
# Only --run-command elevates (via sudo, so it needs this script's path in
# sudoers - see the README): --show-commands is just a list and runs as
# whoever calls it. Everything that does run here is root, so validate every
# arg (see require_host in diagnostics_lib.sh) and never pass one through
# eval/sh -c.
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
SCRIPT_DIR="$(dirname "$SCRIPT")"
# shellcheck source=diagnostics_lib.sh
source "${SCRIPT_DIR}/diagnostics_lib.sh"

# ---- commands ---------------------------------------------------------

register wg-status "" "WireGuard peer status: endpoint, latest handshake, transfer"
cmd_wg_status() {
  "${SCRIPT_DIR}/view_wireguard_status.sh"
}

# ---- dispatch ---------------------------------------------------------

case "${1:-}" in
  --show-commands) show_commands ;;
  --run-command)
    shift
    name="${1:-}"
    [[ -n $name ]] || { echo "usage: $0 --run-command <name> [<arg> ...]" >&2; exit 2; }
    has_command "$name" || { echo "unknown command: $name" >&2; exit 2; }
    [[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" --run-command "$@"
    run_command "$@"
    ;;
  *) echo "usage: $0 --show-commands | --run-command <name> [<arg> ...]" >&2; exit 2 ;;
esac
