#!/usr/bin/env bash
# Usage: view_currently_associated_clients.sh [--ssh]
LEASES="/var/lib/misc/dnsmasq.leases"
SHOW_SSH=0
[[ "${1:-}" == "--ssh" ]] && SHOW_SSH=1

echo "== Devices =="
nmcli -f DEVICE,TYPE,STATE,CONNECTION device status | grep -E '^DEVICE|eth0|wlan|br-ap'

echo; echo "== Default routes (lowest metric is used) =="
ip -4 route show default

if [[ $SHOW_SSH -eq 1 ]]; then
  echo; echo "== SSH sessions (interactive logins) =="
  who -u 2>/dev/null || true

  echo; echo "== SSH connections (established TCP, incl. non-interactive) =="
  sudo ss -tnp state established '( dport = :22 or sport = :22 )' 2>/dev/null \
    || ss -tn state established '( dport = :22 or sport = :22 )'
fi

APS=$(iw dev | awk '$1=="Interface"{i=$2} $1=="type" && $2=="AP"{print i}')

echo; echo "== Access points =="
for ap in $APS; do
  echo "$ap: $(iw dev "$ap" info | awk '/ssid/{print "ssid " $2} /channel/{print "channel " $2}' | paste -sd' ')"
done

echo; printf "%-7s %-18s %-15s %-20s %s\n" IFACE MAC IP HOSTNAME SIGNAL
for ap in $APS; do
  sudo iw dev "$ap" station dump 2>/dev/null \
    | awk '/^Station/{mac=$2} /^[ \t]*signal:/{print mac, $2}' \
    | while read -r mac sig; do
        read -r _ _ ip host _ < <(grep -i "$mac" "$LEASES" || echo "- - ? ?")
        printf "%-7s %-18s %-15s %-20s %s dBm\n" "$ap" "$mac" "${ip:-?}" "${host:-?}" "$sig"
      done
done
