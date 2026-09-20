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
