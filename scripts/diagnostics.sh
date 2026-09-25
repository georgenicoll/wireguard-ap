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
# Runs as the calling user - keep commands to things that don't need root.
# Commands that do live in diagnostics_sudo.sh instead, which speaks this
# same protocol: --show-commands here also lists whatever that script
# lists, and --run-command hands any command not registered here over to
# it. So nothing in this file knows what the root-only commands are.
# Args come from a web form: always validate free text (see require_host)
# rather than trusting it, and never pass it through eval/sh -c.
set -euo pipefail

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"

SUDO_SCRIPT="${SCRIPT_DIR}/diagnostics_sudo.sh"

# shellcheck source=diagnostics_lib.sh
source "${SCRIPT_DIR}/diagnostics_lib.sh"

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

register uname "" "Kernel and system information (uname -a)"
cmd_uname() {
  uname -a
}

register free "" "Memory and swap usage (free -h)"
cmd_free() {
  free -h
}

register os-release "" "Operating system name and version (cat /etc/os-release)"
cmd_os_release() {
  cat /etc/os-release
}

# ---- dispatch ---------------------------------------------------------

case "${1:-}" in
  --show-commands)
    show_commands
    # A missing or failing sudo script just means no root-only commands.
    if [[ -x $SUDO_SCRIPT ]]; then "$SUDO_SCRIPT" --show-commands || true; fi
    ;;
  --run-command)
    shift
    name="${1:-}"
    [[ -n $name ]] || { echo "usage: $0 --run-command <name> [<arg> ...]" >&2; exit 2; }
    if has_command "$name"; then
      run_command "$@"
    elif [[ -x $SUDO_SCRIPT ]]; then
      exec "$SUDO_SCRIPT" --run-command "$@"
    else
      echo "unknown command: $name" >&2
      exit 2
    fi
    ;;
  *) echo "usage: $0 --show-commands | --run-command <name> [<arg> ...]" >&2; exit 2 ;;
esac
