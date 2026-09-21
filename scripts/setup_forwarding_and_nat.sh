#!/usr/bin/env bash
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
ENV_FILE="$(dirname "$SCRIPT")/wireguard-ap.env"
[[ -f $ENV_FILE ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

echo "net.ipv4.ip_forward=1" | tee /etc/sysctl.d/90-mnet-ap.conf >/dev/null
sysctl -w net.ipv4.ip_forward=1

# wg0 is deliberately excluded: wireguard-router's mesh is set up site-to-
# site (wireguard_nat = false there, and this Pi is registered with --lan
# rather than just its own tunnel address), so traffic headed into the
# tunnel keeps each AP client's real source address rather than all of them
# collapsing into the Pi's own tunnel IP. Only the real internet uplink
# (eth0/wlan0) gets masqueraded.
tee /etc/nftables-mnet-ap.conf >/dev/null <<EOF
add table ip mnet_ap
delete table ip mnet_ap
table ip mnet_ap {
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr ${AP_NET} oifname != "${BR}" oifname != "wg0" masquerade
  }
}
EOF

tee /etc/systemd/system/mnet-ap-nat.service >/dev/null <<'EOF'
[Unit]
Description=NAT for mnet-ap AP
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables-mnet-ap.conf
ExecStop=/usr/sbin/nft delete table ip mnet_ap

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mnet-ap-nat.service

# --- keep connections that arrive on eth0/br-ap replying via that same ------
# interface, even if wireguard later adds a route for the same subnet.
#
# Scoped to the nft "input" hook only - connections terminating on the Pi
# itself (SSH, etc), not "forward" - so it never touches AP Wi-Fi clients'
# own traffic, which still resolves via the normal routing table and can
# still be pulled onto wg0 by a colliding peer route (see README). And it's
# keyed on *which interface a connection arrived on*, not on any peer's
# subnet, so it protects management access generically against any current
# or future peer registration without needing to know about peer LANs at
# all - see the incident this addresses in the WireGuard section of the
# README.
tee -a /etc/nftables-mnet-ap.conf >/dev/null <<EOF

add table ip mnet_ap_route
delete table ip mnet_ap_route
table ip mnet_ap_route {
  chain input {
    type filter hook input priority mangle; policy accept;
    iifname "eth0" ct state new ct mark set 0x1
    iifname "${BR}" ct state new ct mark set 0x2
  }
  chain output {
    type route hook output priority mangle; policy accept;
    ct mark != 0x0 meta mark set ct mark
  }
}
EOF

{
  echo '#!/usr/bin/env bash'
  echo '# Regenerates the per-interface routing tables the mnet_ap_route nft'
  echo '# marks above send marked replies through. Safe to re-run.'
  echo 'set -euo pipefail'
  printf 'BR=%q\n' "$BR"
  cat <<'SCRIPT'

setup_table() {
  local mark=$1 table=$2 dev=$3
  ip rule del fwmark "$mark" table "$table" 2>/dev/null || true
  ip route flush table "$table" 2>/dev/null || true
  [[ -e /sys/class/net/$dev ]] || return 0
  local gw
  gw="$(ip route show default dev "$dev" 2>/dev/null | awk '{print $3; exit}')"
  if [[ -n $gw ]]; then
    ip route add default via "$gw" dev "$dev" table "$table"
  else
    ip route add default dev "$dev" table "$table"
  fi
  ip rule add fwmark "$mark" table "$table" priority 100
}

setup_table 0x1 101 eth0
setup_table 0x2 102 "$BR"
SCRIPT
} | tee /usr/local/sbin/mnet-ap-local-routing.sh >/dev/null
chmod 755 /usr/local/sbin/mnet-ap-local-routing.sh

tee /etc/systemd/system/mnet-ap-local-routing.service >/dev/null <<'EOF'
[Unit]
Description=Per-interface reply routing for mnet-ap local connections
After=network-online.target mnet-ap-nat.service
Wants=network-online.target
Requires=mnet-ap-nat.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/mnet-ap-local-routing.sh
ExecStop=/bin/true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mnet-ap-local-routing.service
systemctl restart mnet-ap-local-routing.service
