#!/usr/bin/env bash
# Usage: setup_host.sh <reg_domain>
# One-time-ish host setup (section 3 of the handoff doc): packages, stock
# services, Wi-Fi country, radio unblock. Idempotent - safe to re-run,
# including as part of every apply.
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
REG_DOMAIN="${1:?usage: $0 <reg_domain>}"

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

apt-get update -y
apt-get install -y hostapd dnsmasq nftables iw wireguard-tools openssl traceroute dnsutils acl
systemctl disable --now hostapd 2>/dev/null || true
systemctl enable dnsmasq
systemctl enable --now ssh
raspi-config nonint do_wifi_country "$REG_DOMAIN"
rfkill unblock wlan
