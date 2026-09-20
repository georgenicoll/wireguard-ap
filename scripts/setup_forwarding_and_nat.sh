#!/usr/bin/env bash
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
ENV_FILE="$(dirname "$SCRIPT")/wireguard-ap.env"
[[ -f $ENV_FILE ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/90-mnet-ap.conf >/dev/null
sudo sysctl -w net.ipv4.ip_forward=1

sudo tee /etc/nftables-mnet-ap.conf >/dev/null <<EOF
add table ip mnet_ap
delete table ip mnet_ap
table ip mnet_ap {
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr ${AP_NET} oifname != "${BR}" masquerade
  }
}
EOF

sudo tee /etc/systemd/system/mnet-ap-nat.service >/dev/null <<'EOF'
[Unit]
Description=NAT for mnet-on-the-road AP
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

sudo systemctl daemon-reload
sudo systemctl enable --now mnet-ap-nat.service
