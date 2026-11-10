#!/usr/bin/env bash
set -euo pipefail

SCRIPT="$(readlink -f "$0")"
ENV_FILE="$(dirname "$SCRIPT")/wireguard-ap.env"
[[ -f $ENV_FILE ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

# --- the per-interface reply routing: its script, and the hook that re-runs it
# The nft rules further down mark connections by the interface they arrived on
# (eth0 = 0x1, the AP bridge = 0x2); these send each mark's replies out of the
# same interface, through a routing table of its own (101, 102) holding just
# a default route via that interface's gateway.
#
# That table is only as good as its last fill. Its route goes away with the
# interface (a link flap, a new DHCP lease on a different network, moving the
# Pi to another switch port or VLAN), while the rule pointing at the table
# stays - and replies to a connection that came in on eth0 then fall through
# to the main table, which can send them into wg0 (a peer's routed LAN
# colliding with where the Pi is plugged in). SSH and the web app from that
# network then time out, while the Pi still looks healthy from anywhere else.
# So the script is safe to run at any moment (it updates in place, and never
# leaves the table empty or the rule doubled), and a NetworkManager dispatcher
# hook runs it on every network event, not only once at boot.
#
#   setup_forwarding_and_nat.sh --print-routing-script   prints the script
#   setup_forwarding_and_nat.sh --print-dispatcher       prints the hook
#                                  Each exits without needing root: for tests.
render_routing_script() {
  echo '#!/usr/bin/env bash'
  echo '# Fills the per-interface routing tables the mnh_ap_route nft marks send'
  echo '# replies through (see setup_forwarding_and_nat.sh). Safe to re-run at any'
  echo '# time: it updates in place and is run again on every network change.'
  echo 'set -euo pipefail'
  printf 'BR=%q\n' "$BR"
  cat <<'SCRIPT'

setup_table() {
  local mark=$1 table=$2 dev=$3
  if ! ip link show dev "$dev" >/dev/null 2>&1; then
    # No such interface (yet): nothing to send replies through.
    ip rule del fwmark "$mark" table "$table" 2>/dev/null || true
    ip route flush table "$table" 2>/dev/null || true
    return 0
  fi
  local gw
  gw="$(ip route show default dev "$dev" 2>/dev/null | awk '{print $3; exit}')"
  # replace, not flush-then-add: a reply sent in between must never find the
  # table empty. If the route can't be set (the interface is down) the table
  # is emptied instead, rather than left pointing at a gateway that has gone.
  if [[ -n $gw ]]; then
    ip route replace default via "$gw" dev "$dev" table "$table" 2>/dev/null \
      || ip route flush table "$table" 2>/dev/null || true
  else
    ip route replace default dev "$dev" table "$table" 2>/dev/null \
      || ip route flush table "$table" 2>/dev/null || true
  fi
  # Added once: `ip rule add` happily adds the same rule again.
  if ! ip rule show | grep -qE "fwmark ${mark} lookup ${table}( |$)"; then
    ip rule add fwmark "$mark" table "$table" priority 100
  fi
}

setup_table 0x1 101 eth0
setup_table 0x2 102 "$BR"
SCRIPT
}

render_dispatcher() {
  cat <<'HOOK'
#!/usr/bin/env bash
# Installed by setup_forwarding_and_nat.sh. NetworkManager runs this on every
# network event ($1 = interface, $2 = event): re-fill the per-interface reply
# routing tables, which lose their route when an interface is reconfigured.
# The events that fire before the interface is configured are skipped: the
# gateway isn't there yet, and the "up" that follows is the one that matters.
case "${2:-}" in
  pre-up | pre-down | hostname) exit 0 ;;
esac
exec /usr/local/sbin/mnh-ap-local-routing.sh
HOOK
}

case "${1:-}" in
  --print-routing-script) render_routing_script; exit 0 ;;
  --print-dispatcher) render_dispatcher; exit 0 ;;
esac

[[ $EUID -eq 0 ]] || exec sudo "$SCRIPT" "$@"

echo "net.ipv4.ip_forward=1" | tee /etc/sysctl.d/90-mnh-ap.conf >/dev/null
sysctl -w net.ipv4.ip_forward=1

# === write everything first, then load it all at the end (in dependency ====
# order) - writing config and reloading a service interleaved is how the
# mnh_ap_route table below once went missing: mnh-ap-nat.service got
# restarted while the file only had the NAT table on disk, and nothing
# reloaded it again after the rest was appended.

# wg0 is deliberately excluded: wireguard-router's mesh is set up site-to-
# site (wireguard_nat = false there, and this Pi is registered with --lan
# rather than just its own tunnel address), so traffic headed into the
# tunnel keeps each AP client's real source address rather than all of them
# collapsing into the Pi's own tunnel IP. Only the real internet uplink
# (eth0/wlan0) gets masqueraded.
tee /etc/nftables-mnh-ap.conf >/dev/null <<EOF
add table ip mnh_ap
delete table ip mnh_ap
table ip mnh_ap {
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr ${AP_NET} oifname != "${BR}" oifname != "wg0" masquerade
  }
}
EOF

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
#
# Deliberately unconditional (no "ct state new" restriction): marking only
# brand new connections would never protect one already open when this rule
# is (re)loaded - e.g. the SSH session running this very apply, or any
# session open from before wg0 last came up - since a mark only applies from
# the point a matching packet is actually seen. Re-marking every inbound
# packet on every run is cheap and covers that case too: the next inbound
# packet of an already-established connection gets it (re-)tagged just the
# same as a new one would.
tee -a /etc/nftables-mnh-ap.conf >/dev/null <<EOF

add table ip mnh_ap_route
delete table ip mnh_ap_route
table ip mnh_ap_route {
  chain input {
    type filter hook input priority mangle; policy accept;
    iifname "eth0" ct mark set 0x1
    iifname "${BR}" ct mark set 0x2
  }
  chain output {
    type route hook output priority mangle; policy accept;
    ct mark != 0x0 meta mark set ct mark
  }
}
EOF

tee /etc/systemd/system/mnh-ap-nat.service >/dev/null <<'EOF'
[Unit]
Description=NAT for mnh-ap AP
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables-mnh-ap.conf
ExecStop=/bin/sh -c '/usr/sbin/nft delete table ip mnh_ap 2>/dev/null; /usr/sbin/nft delete table ip mnh_ap_route 2>/dev/null; true'

[Install]
WantedBy=multi-user.target
EOF

render_routing_script | tee /usr/local/sbin/mnh-ap-local-routing.sh >/dev/null
chmod 755 /usr/local/sbin/mnh-ap-local-routing.sh

# Re-run on network events, not only at boot (see the comment at the top).
install -d -m 755 /etc/NetworkManager/dispatcher.d
render_dispatcher | tee /etc/NetworkManager/dispatcher.d/91-mnh-ap-local-routing >/dev/null
chmod 755 /etc/NetworkManager/dispatcher.d/91-mnh-ap-local-routing

tee /etc/systemd/system/mnh-ap-local-routing.service >/dev/null <<'EOF'
[Unit]
Description=Per-interface reply routing for mnh-ap local connections
After=network-online.target mnh-ap-nat.service
Wants=network-online.target
Requires=mnh-ap-nat.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/mnh-ap-local-routing.sh
ExecStop=/bin/true

[Install]
WantedBy=multi-user.target
EOF

# === now load everything, in dependency order ===============================
systemctl daemon-reload

systemctl enable mnh-ap-nat.service
# "enable --now" is a no-op if the unit is already active from a previous
# apply, which would silently skip re-reading an updated
# /etc/nftables-mnh-ap.conf - explicit restart instead, every time.
systemctl restart mnh-ap-nat.service

systemctl enable mnh-ap-local-routing.service
systemctl restart mnh-ap-local-routing.service
