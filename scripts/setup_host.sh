#!/usr/bin/env bash
# Usage: setup_host.sh <reg_domain>
# One-time-ish host setup (section 3 of the handoff doc): packages, stock
# services, Wi-Fi country, radio unblock. Idempotent - safe to re-run,
# including as part of every apply (which, once the packages are there, does
# not touch the network or upgrade anything).
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
REG_DOMAIN="${1:?usage: $0 <reg_domain>}"

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

PACKAGES=(hostapd dnsmasq nftables iw wireguard-tools openssl traceroute dnsutils acl)

# The network is only needed when a package is missing, and nothing already
# installed is upgraded here. A plain `apt-get install` of packages that are
# already there quietly upgrades them to the newest version, on every deploy:
# it upgraded hostapd, openssl and bind9 in the middle of an unrelated apply,
# and a deploy that merely re-runs the setup then depends on every mirror being
# reachable (it hung on one that wasn't, mid-apply). Keeping the system up to
# date is `apt upgrade`'s job, run when you choose to.
needs_install() {
  # What apt *would* do with these packages and the indexes it already has.
  # If it can't even work that out (a fresh Pi with no indexes yet), install.
  local plan
  plan="$(apt-get -s install -y --no-upgrade "$@" 2>&1)" || return 0
  grep -q '^Inst ' <<<"$plan"
}

if needs_install "${PACKAGES[@]}"; then
  apt-get update -y
  apt-get install -y --no-upgrade "${PACKAGES[@]}"
fi
systemctl disable --now hostapd 2>/dev/null || true
systemctl enable dnsmasq
systemctl enable --now ssh
raspi-config nonint do_wifi_country "$REG_DOMAIN"
rfkill unblock wlan
