#!/usr/bin/env bash
# Brings up this Pi's WireGuard uplink to wireguard-router, from a client
# config staged alongside this script. Safe to re-run: always re-installs
# the config and restarts the unit, so a changed config actually takes
# effect rather than being ignored because the unit was already running.
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
CONF_STAGED="$(dirname "$SCRIPT")/.wg0.conf"
[[ -f $CONF_STAGED ]] || { echo "missing staged config: $CONF_STAGED" >&2; exit 1; }

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

install -o root -g root -m 0600 "$CONF_STAGED" /etc/wireguard/wg0.conf
rm -f "$CONF_STAGED"

systemctl enable wg-quick@wg0
systemctl restart wg-quick@wg0

# --- make wg0 the deterministic winner for any prefix it shares with a ------
# locally-connected interface (eth0/wlan0/br-ap), instead of leaving it to
# whichever route the kernel happens to prefer. This only affects traffic
# that isn't already pinned by setup_forwarding_and_nat.sh's interface-based
# rules (SSH/management access into the Pi itself, which stays on the
# interface it arrived on regardless of this) - i.e. AP clients' own
# traffic, and any new connection the Pi itself initiates. See README.
tee /usr/local/sbin/mnh-ap-wg-route-precedence.sh >/dev/null <<'SCRIPT'
#!/usr/bin/env bash
# Re-run whenever wg0 comes up and on every NetworkManager event (a DHCP
# renewal on eth0/wlan0 can reinstall its connected route at the default
# metric, undoing this). Safe to re-run; no-ops if wg0 has no routes yet.
set -euo pipefail

ip link show wg0 &>/dev/null || exit 0

while IFS= read -r prefix; do
    [[ -z $prefix ]] && continue
    ip route replace "$prefix" dev wg0 metric 0 2>/dev/null || true
    while IFS= read -r dev; do
        [[ -z $dev || $dev == wg0 ]] && continue
        # Assumes the competing route is a plain directly-connected/on-link
        # entry (true for eth0/wlan0/br-ap's own LANs, the only case this
        # is meant to cover) - a replace here would drop a gateway ("via")
        # on anything else.
        ip route replace "$prefix" dev "$dev" metric 900 2>/dev/null || true
    done < <(ip route show to exact "$prefix" | awk '{for (i=1;i<=NF;i++) if ($i=="dev") print $(i+1)}')
done < <(ip route show dev wg0 | awk '{print $1}')
SCRIPT
chmod 755 /usr/local/sbin/mnh-ap-wg-route-precedence.sh

tee /etc/NetworkManager/dispatcher.d/90-mnh-ap-wg-route-precedence >/dev/null <<'EOF'
#!/usr/bin/env bash
exec /usr/local/sbin/mnh-ap-wg-route-precedence.sh
EOF
chmod 755 /etc/NetworkManager/dispatcher.d/90-mnh-ap-wg-route-precedence

/usr/local/sbin/mnh-ap-wg-route-precedence.sh

# --- re-resolve the peer's endpoint hostname once its handshake goes stale --
# Ported from OpenWRT's wireguard_watchdog (Jason A. Donenfeld / Aleksandr V.
# Piskunov, GPL-2.0) - see /usr/bin/wireguard_watchdog on an OpenWRT router.
# wg-quick only resolves Endpoint once, at startup; if a dynamic-DNS
# hostname's IP changes afterward the tunnel silently keeps talking to the
# old address until something re-resolves it (this happened for real: see
# git history around this comment). Re-resolving via "wg set ... endpoint
# host:port" updates just that peer in place - no interface restart, no
# route flap - unlike this file's own DNS change, which does need a
# restart because it's *this Pi's* address that changed, not the peer's.
tee /usr/local/sbin/mnh-ap-wg-watchdog.sh >/dev/null <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
IFACE=wg0
STALE_AFTER=150 # seconds - matches the upstream OpenWRT script's default

ip link show "$IFACE" &>/dev/null || exit 0

for peer in $(wg show "$IFACE" peers 2>/dev/null); do
  keepalive="$(wg show "$IFACE" persistent-keepalive | awk -v p="$peer" '$1==p{print $2}')"
  [[ -z $keepalive || $keepalive == off ]] && continue

  endpoint="$(awk '/^\[Peer\]/{p=1} p && /^Endpoint *=/{print $3; exit}' "/etc/wireguard/${IFACE}.conf")"
  [[ -z $endpoint ]] && continue
  host="${endpoint%:*}"
  port="${endpoint##*:}"

  # nothing to re-resolve if the config already names a literal address
  [[ $host =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && continue

  last_handshake="$(wg show "$IFACE" latest-handshakes | awk -v p="$peer" '$1==p{print $2}')"
  [[ -z $last_handshake ]] && continue
  idle=$(($(date +%s) - last_handshake))
  ((idle < STALE_AFTER)) && continue

  logger -t mnh-ap-wg-watchdog "$IFACE endpoint $host:$port idle ${idle}s, re-resolving"
  wg set "$IFACE" peer "$peer" endpoint "$host:$port"
done
SCRIPT
chmod 755 /usr/local/sbin/mnh-ap-wg-watchdog.sh

tee /etc/systemd/system/mnh-ap-wg-watchdog.service >/dev/null <<'EOF'
[Unit]
Description=Re-resolve wg0's peer endpoint if its handshake goes stale

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/mnh-ap-wg-watchdog.sh
EOF

tee /etc/systemd/system/mnh-ap-wg-watchdog.timer >/dev/null <<'EOF'
[Unit]
Description=Run mnh-ap-wg-watchdog every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now mnh-ap-wg-watchdog.timer
