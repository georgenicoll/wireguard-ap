#!/usr/bin/env bash
# Diagnostic commands for the web UI's Diagnostics page (see webapp/app.py).
#
#   diagnostics.sh --show-commands
#       One line per command: "<name> [<param> ...] | <description>". The
#       web UI builds the page from this, so it never needs to know what
#       commands exist.
#   diagnostics.sh --run-command <name> [<arg> ...]
#       Runs that command with one arg per declared param.
#
# To add a command: write a cmd_<name> function (dashes in the command name
# become underscores) that reads its params from "$1", "$2", ..., and add a
# "register" line for it below. That's all - --show-commands and
# --run-command both pick it up from the registry.
#
# Runs as the calling user (no self-elevation, so no sudoers entry needed) -
# keep commands to things that don't need root. Args come from a web form:
# always validate them (see require_host) rather than trusting them, and
# never pass them through eval/sh -c.
set -euo pipefail

declare -A PARAMS DESCRIPTIONS
COMMAND_NAMES=()

# register <name> "<space-separated param names>" "<description>"
register() {
  COMMAND_NAMES+=("$1")
  PARAMS["$1"]="$2"
  DESCRIPTIONS["$1"]="$3"
}

# Hostname or IPv4/IPv6 address only. In particular can't start with "-",
# so a value can never be mistaken for an option by the tool it's given to.
require_host() {
  if [[ ! $1 =~ ^[A-Za-z0-9][A-Za-z0-9._:-]*$ ]]; then
    echo "invalid host: '$1'" >&2
    exit 2
  fi
}

require_tool() {
  command -v "$1" >/dev/null || { echo "$1 is not installed" >&2; exit 127; }
}

# ---- commands ---------------------------------------------------------

register ping "host" "Ping a host or IP address until stopped (close the output dialog to stop)"
cmd_ping() {
  require_host "$1"
  ping -W 3 "$1"
}

register traceroute "host" "Trace the route packets take to a host or IP address"
cmd_traceroute() {
  require_host "$1"
  require_tool traceroute
  traceroute -w 2 -m 20 "$1"
}

register ip-addr-list "" "List all network interfaces and their IP addresses"
cmd_ip_addr_list() {
  ip addr show
}

# ---- dispatch ---------------------------------------------------------

show_commands() {
  local name
  for name in "${COMMAND_NAMES[@]}"; do
    # Unquoted on purpose: collapses an empty param list to nothing.
    echo "${name}${PARAMS[$name]:+ ${PARAMS[$name]}} | ${DESCRIPTIONS[$name]}"
  done
}

run_command() {
  local name="${1:-}"
  [[ -n $name ]] || { echo "usage: $0 --run-command <name> [<arg> ...]" >&2; exit 2; }
  shift
  [[ -v PARAMS[$name] ]] || { echo "unknown command: $name" >&2; exit 2; }

  local -a expected
  read -r -a expected <<<"${PARAMS[$name]}"
  if (($# != ${#expected[@]})); then
    echo "$name takes ${#expected[@]} parameter(s) (${PARAMS[$name]:-none}), got $#" >&2
    exit 2
  fi

  "cmd_${name//-/_}" "$@"
}

case "${1:-}" in
  --show-commands) show_commands ;;
  --run-command) shift; run_command "$@" ;;
  *) echo "usage: $0 --show-commands | --run-command <name> [<arg> ...]" >&2; exit 2 ;;
esac
