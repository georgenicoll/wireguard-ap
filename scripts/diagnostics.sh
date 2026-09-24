#!/usr/bin/env bash
# Diagnostic commands for the web UI's Diagnostics page (see webapp/app.py).
#
#   diagnostics.sh --show-commands
#       One line per command: "<name> [<param> ...] | <description>". The
#       web UI builds the page from this, so it never needs to know what
#       commands exist. Each <param> is a name, optionally followed by:
#         ?           the value is optional (may be blank)
#         =a,b,c      the value must be one of these choices
#       e.g. "host", "server?", "unit=dnsmasq,wg-quick@wg0", "type?=A,MX".
#       Names and choices are letters, digits and - _ . @ : only.
#   diagnostics.sh --run-command <name> [<arg> ...]
#       Runs that command with exactly one arg per declared param - "" for
#       an optional param left blank. The script itself rejects a blank
#       required param and any value outside a param's choices.
#
# To add a command: write a cmd_<name> function (dashes in the command name
# become underscores) that reads its params from "$1", "$2", ..., and add a
# "register" line for it below. That's all - --show-commands and
# --run-command both pick it up from the registry. The registry is rebuilt
# on every call, so a choice list can be computed at runtime too.
#
# Runs as the calling user (no self-elevation, so no sudoers entry needed) -
# keep commands to things that don't need root. Anything that does is a
# separate self-elevating script (see wg-status, ap-clients), which already
# has its own sudoers entry. Args come from a web form: always validate
# free text (see require_host) rather than trusting it, and never pass it
# through eval/sh -c.
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"

declare -A PARAMS DESCRIPTIONS
COMMAND_NAMES=()

# register <name> "<space-separated params>" "<description>"
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

# Units the service-status and logs commands can look at. A fixed list (not
# free text) so they can't be pointed at anything else on the system.
UNIT_CHOICES="mnh-ap-webapp,mnh-ap-nat,mnh-ap-local-routing,mnh-ap-wg-watchdog.service,mnh-ap-wg-watchdog.timer,mnh-hostapd@wlan0,mnh-hostapd@wlan1,dnsmasq,wg-quick@wg0,NetworkManager"

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

register dig "name server? type?=A,AAAA,MX,TXT,NS,CNAME,SOA" "Look up a DNS name (optionally against a specific server, e.g. 1.1.1.1, and record type)"
cmd_dig() {
  local name="$1" server="$2" type="$3"
  require_host "$name"
  local -a args=()
  if [[ -n $server ]]; then
    require_host "$server"
    args+=("@${server}")
  fi
  require_tool dig
  args+=("$name")
  [[ -n $type ]] && args+=("$type")
  dig "${args[@]}"
}

register ip-addr-list "" "List all network interfaces and their IP addresses"
cmd_ip_addr_list() {
  ip addr show
}

register ip-route-list "" "Show the routing table and routing policy rules"
cmd_ip_route_list() {
  echo "# ip route"
  ip route show
  echo
  echo "# ip rule"
  ip rule show
}

register ip-route-get "dest" "Show which route and interface traffic to a host or IP address would use"
cmd_ip_route_get() {
  require_host "$1"
  ip route get "$1"
}

register wg-status "" "WireGuard peer status: endpoint, latest handshake, transfer"
cmd_wg_status() {
  "${SCRIPT_DIR}/view_wireguard_status.sh"
}

register ap-clients "" "Wi-Fi devices currently associated with the access point"
cmd_ap_clients() {
  "${SCRIPT_DIR}/view_currently_associated_clients.sh"
}

register service-status "unit=${UNIT_CHOICES}" "Status of one of this project's services, followed by any failed units"
cmd_service_status() {
  # systemctl exits non-zero for an inactive/failed unit - that's the
  # answer here, not an error, so it mustn't stop the failed-units list.
  systemctl status --no-pager --lines=15 "$1" || true
  echo
  echo "# failed units"
  systemctl --failed --no-pager
}

register logs "unit=${UNIT_CHOICES} lines=50,100,500" "Most recent journal entries for one of this project's services"
cmd_logs() {
  journalctl -u "$1" -n "$2" --no-pager
}

# ---- dispatch ---------------------------------------------------------

show_commands() {
  local name
  for name in "${COMMAND_NAMES[@]}"; do
    # Unquoted on purpose: collapses an empty param list to nothing.
    echo "${name}${PARAMS[$name]:+ ${PARAMS[$name]}} | ${DESCRIPTIONS[$name]}"
  done
}

# Checks one arg against its param spec ("name", "name?", "name=a,b" ...).
check_param() {
  local spec="$1" value="$2"
  local name="${spec%%[?=]*}" rest="${spec#"${spec%%[?=]*}"}"
  local optional=0 choices=""
  [[ $rest == \?* ]] && { optional=1; rest="${rest#\?}"; }
  [[ $rest == =* ]] && choices="${rest#=}"

  if [[ -z $value ]]; then
    ((optional)) || { echo "$name is required" >&2; exit 2; }
    return 0
  fi
  if [[ -n $choices && ",${choices}," != *",${value},"* ]]; then
    echo "$name must be one of: ${choices//,/, } (got '$value')" >&2
    exit 2
  fi
}

run_command() {
  local name="${1:-}"
  [[ -n $name ]] || { echo "usage: $0 --run-command <name> [<arg> ...]" >&2; exit 2; }
  shift
  [[ -v PARAMS[$name] ]] || { echo "unknown command: $name" >&2; exit 2; }

  local -a specs
  read -r -a specs <<<"${PARAMS[$name]}"
  if (($# != ${#specs[@]})); then
    echo "$name takes ${#specs[@]} parameter(s) (${PARAMS[$name]:-none}), got $#" >&2
    exit 2
  fi

  local i
  for i in "${!specs[@]}"; do
    check_param "${specs[$i]}" "${@:$((i + 1)):1}"
  done

  "cmd_${name//-/_}" "$@"
}

case "${1:-}" in
  --show-commands) show_commands ;;
  --run-command) shift; run_command "$@" ;;
  *) echo "usage: $0 --show-commands | --run-command <name> [<arg> ...]" >&2; exit 2 ;;
esac
