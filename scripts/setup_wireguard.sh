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
tee /usr/local/sbin/mnet-ap-wg-route-precedence.sh >/dev/null <<'SCRIPT'
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
chmod 755 /usr/local/sbin/mnet-ap-wg-route-precedence.sh

tee /etc/NetworkManager/dispatcher.d/90-mnet-ap-wg-route-precedence >/dev/null <<'EOF'
#!/usr/bin/env bash
exec /usr/local/sbin/mnet-ap-wg-route-precedence.sh
EOF
chmod 755 /etc/NetworkManager/dispatcher.d/90-mnet-ap-wg-route-precedence

/usr/local/sbin/mnet-ap-wg-route-precedence.sh
