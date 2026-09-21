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

# Drop any AllowedIPs entry that overlaps a subnet already reachable via
# another interface (a directly-connected route) before wg0 exists. This Pi
# roams, so which peer's routed LAN (if any) collides with wherever it's
# physically plugged in right now can't be known in advance or hardcoded -
# routing such a subnet via wg0 anyway broke eth0 connectivity outright the
# first time this ran. Anything dropped here is already reachable directly
# via its own interface, with no loss.
export LOCAL_SUBNETS="$(ip -4 route show scope link | awk '{print $1}')"

FILTERED_ALLOWED_IPS="$(python3 - "$CONF_STAGED" <<'PYEOF'
import ipaddress, os, re, sys

conf_path = sys.argv[1]
local_subnets = [ipaddress.ip_network(s) for s in os.environ["LOCAL_SUBNETS"].split()]

with open(conf_path) as f:
    conf = f.read()

m = re.search(r'^AllowedIPs\s*=\s*(.+)$', conf, re.MULTILINE)
if not m:
    sys.exit(0)

kept = []
for entry in m.group(1).split(","):
    entry = entry.strip()
    net = ipaddress.ip_network(entry, strict=False)
    if any(net.overlaps(local) for local in local_subnets):
        print(f"warning: dropping {entry} from AllowedIPs - overlaps a locally-connected subnet", file=sys.stderr)
        continue
    kept.append(entry)

print(", ".join(kept), end="")
PYEOF
)"

[[ -n "$FILTERED_ALLOWED_IPS" ]] || {
  echo "error: every AllowedIPs entry overlaps a local subnet - refusing to bring up wg0 with no safe routes" >&2
  exit 1
}

sed -i -E "s|^AllowedIPs[[:space:]]*=.*|AllowedIPs = ${FILTERED_ALLOWED_IPS}|" "$CONF_STAGED"

install -o root -g root -m 0600 "$CONF_STAGED" /etc/wireguard/wg0.conf
rm -f "$CONF_STAGED"

systemctl enable wg-quick@wg0
systemctl restart wg-quick@wg0
