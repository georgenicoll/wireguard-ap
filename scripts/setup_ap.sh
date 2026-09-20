#!/usr/bin/env bash
# Usage:
#   setup_ap.sh dual             wlan0 = 2.4 GHz AP + wlan1 = 5 GHz AP (one bridged network)
#   setup_ap.sh uplink [5|2.4]   wlan1 = the only AP (default 5 GHz), wlan0 = Wi-Fi uplink
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
MODE="${1:-}"
BAND="${2:-5}"
case "$MODE" in dual|uplink) ;; *) echo "usage: $0 dual | uplink [5|2.4]" >&2; exit 1 ;; esac
case "$BAND" in 5|2.4) ;; *) echo "band must be 5 or 2.4" >&2; exit 1 ;; esac
if [[ $MODE == dual ]]; then BAND=5; fi

[[ $EUID -eq 0 ]] || exec sudo --preserve-env=SSH_CONNECTION "$SCRIPT" "$@"

ENV_FILE="$(dirname "$SCRIPT")/wireguard-ap.env"
[[ -f $ENV_FILE ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

# --- if this SSH session is over Wi-Fi, carry on in the background ----------
if [[ -z "${MNET_DETACHED:-}" && -n "${SSH_CONNECTION:-}" ]]; then
  VIA=$(ip -o route get "${SSH_CONNECTION%% *}" | sed -n 's/.* dev \([^ ]*\).*/\1/p' || true)
  if [[ "$VIA" == "$BR" || "$VIA" == wlan* ]]; then
    echo "Your SSH session is over Wi-Fi ($VIA) and will drop while this runs."
    echo "Continuing in the background. Reconnect in about a minute. Log: journalctl -u mnet-setup"
    exec systemd-run --unit=mnet-setup --collect --setenv=MNET_DETACHED=1 "$SCRIPT" "$@"
  fi
fi

drv() { local p="/sys/class/net/$1/device/driver"; [[ -e $p ]] && basename "$(readlink -f "$p")" || echo "not present"; }
echo "Mode: $MODE | $IF_24: $(drv "$IF_24") (expect brcmfmac) | $IF_5: $(drv "$IF_5") (expect mt7921u)"

# --- hostapd config writer: iface band channel -------------------------------
write_hostapd() {
  local iface=$1 band=$2 ch=$3 radio ht
  if [[ $band == 5 ]]; then
    case "$ch" in
      36|44) ht="[HT40+]" ;;
      40|48) ht="[HT40-]" ;;
      *) echo "5 GHz channel must be 36, 40, 44 or 48" >&2; exit 1 ;;
    esac
    radio="hw_mode=a
ieee80211n=1
ht_capab=${ht}[SHORT-GI-40]
ieee80211ac=1
vht_oper_chwidth=1
vht_oper_centr_freq_seg0_idx=42"
  else
    radio="hw_mode=g
ieee80211n=1"
  fi
  ( umask 077; cat >"/etc/hostapd/mnet-${iface}.conf" <<EOF
interface=${iface}
bridge=${BR}
driver=nl80211
ctrl_interface=/run/hostapd
ssid=${SSID}
country_code=${REG_DOMAIN}
ieee80211d=1
channel=${ch}
${radio}
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=${PSK}
EOF
  )
}

mkdir -p /etc/hostapd
if [[ $BAND == 5 ]]; then CH_1=$CH_5; else CH_1=$CH_24; fi
write_hostapd "$IF_5" "$BAND" "$CH_1"

if [[ $MODE == dual ]]; then
  write_hostapd "$IF_24" 2.4 "$CH_24"
else
  # give wlan0 back to NetworkManager as a client
  systemctl disable --now "mnet-hostapd@${IF_24}" 2>/dev/null || true
  rm -f "/etc/hostapd/mnet-${IF_24}.conf"
  iw dev "$IF_24" set type managed 2>/dev/null || true
fi

# --- NetworkManager: only the bridge (and wlan0 in uplink mode) -------------
UNMANAGED="interface-name:${IF_5}"
if [[ $MODE == dual ]]; then UNMANAGED="interface-name:${IF_24};${UNMANAGED}"; fi

cat >/etc/NetworkManager/conf.d/90-mnet-ap.conf <<EOF
[main]
ignore-carrier=interface-name:${BR}

[keyfile]
unmanaged-devices=${UNMANAGED}
EOF
nmcli general reload

nmcli con delete mnet-on-the-road 2>/dev/null || true    # old single-radio profile
nmcli con delete "$BR" 2>/dev/null || true
nmcli con add type bridge con-name "$BR" ifname "$BR" \
  bridge.stp no \
  ipv4.method manual \
  ipv4.addresses "${AP_IP}/24" \
  ipv6.method disabled \
  connection.autoconnect yes
nmcli con up "$BR" || true

# --- hostapd unit: starts when the interface appears (hotplug-safe) ---------
cat >/etc/systemd/system/mnet-hostapd@.service <<'EOF'
[Unit]
Description=hostapd for mnet-on-the-road on %i
BindsTo=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
ExecStartPre=-/usr/sbin/rfkill unblock wlan
ExecStartPre=/bin/sh -c 'for i in $$(seq 1 30); do ip link show br-ap >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
ExecStart=/usr/sbin/hostapd /etc/hostapd/mnet-%i.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=sys-subsystem-net-devices-%i.device
EOF

# --- dnsmasq on the bridge ---------------------------------------------------
# Clients get the AP as DNS; dnsmasq forwards to /etc/resolv.conf (eth0's servers first).
rm -f /etc/dnsmasq.d/mnet-on-the-road.conf
cat >/etc/dnsmasq.d/mnet-on-the-road.conf <<EOF
interface=${BR}
bind-dynamic
dhcp-authoritative
dhcp-range=${DHCP_START},${DHCP_END},${AP_NETMASK},${LEASE}
dhcp-option=option:router,${AP_IP}
strict-order
EOF

mkdir -p /etc/systemd/system/dnsmasq.service.d
cat >/etc/systemd/system/dnsmasq.service.d/mnet-ap.conf <<'EOF'
[Unit]
After=NetworkManager.service

[Service]
Restart=on-failure
RestartSec=5
EOF

# --- start everything --------------------------------------------------------
systemctl daemon-reload
AP_IFACES=("$IF_5")
if [[ $MODE == dual ]]; then AP_IFACES+=("$IF_24"); fi
for i in "${AP_IFACES[@]}"; do
  systemctl enable "mnet-hostapd@${i}"
  if [[ -e "/sys/class/net/$i" ]]; then
    systemctl restart "mnet-hostapd@${i}"
  else
    echo "$i is not present right now; its AP will start as soon as it appears."
  fi
done
systemctl enable dnsmasq
systemctl restart dnsmasq

echo "Done ($MODE). Join '${SSID}' and run: ssh ${PI_USER}@${AP_IP}"
