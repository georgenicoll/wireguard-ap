#!/usr/bin/env bash
# Usage: uplink_wifi.sh "SSID" ["password"]     (omit the password for an open network)
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
SSID="${1:?usage: $0 SSID [password]}"
PSK="${2:-}"
CON="uplink-${SSID}"

if nmcli -g GENERAL.STATE device show wlan0 | grep -q unmanaged; then
  echo "wlan0 is unmanaged (dual mode). Run: ./setup_ap.sh uplink" >&2
  exit 1
fi

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

args=(type wifi con-name "$CON" ifname wlan0 ssid "$SSID"
      connection.autoconnect yes ipv4.route-metric 600 ipv6.route-metric 600)
if [[ -n "$PSK" ]]; then
  args+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK")
fi

nmcli con delete "$CON" 2>/dev/null || true
nmcli con add "${args[@]}"
nmcli con up "$CON"
