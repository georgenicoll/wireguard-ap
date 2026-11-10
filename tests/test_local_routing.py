"""Replies to connections that arrive on eth0 (SSH, the web app) must keep
leaving through eth0, however the network around the Pi changes. Checked
without a Pi, against the real scripts, in a throwaway network namespace with
dummy interfaces standing in for eth0, the AP bridge and wg0. Tier: local.

The failure this guards against really happened: after the Pi was moved to
another network, the per-interface routing table for eth0 was empty (its route
had gone with the old network and nothing filled it again), so replies to the
home LAN fell through to wg0, where a peer routes that LAN, and every SSH and
web connection from it timed out while the Pi looked healthy from anywhere else.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from support import SCRIPTS

SETUP = SCRIPTS / "setup_forwarding_and_nat.sh"


def _netns_available() -> bool:
    try:
        r = subprocess.run(["unshare", "-rn", "ip", "link", "add", "t0", "type", "dummy"],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


pytestmark = [
    pytest.mark.local,
    pytest.mark.skipif(not _netns_available(),
                       reason="needs unprivileged network namespaces (unshare -rn) and iproute2"),
]


@pytest.fixture
def tools(tmp_path) -> dict[str, Path]:
    """The routing script and the NetworkManager hook, as setup_forwarding_and_nat.sh
    would install them (printed without root), with the hook pointed at the copy
    in tmp_path instead of /usr/local/sbin."""
    shutil.copy(SETUP, tmp_path / "setup_forwarding_and_nat.sh")
    (tmp_path / "wireguard-ap.env").write_text("BR=br-ap\n")

    def render(what: str) -> str:
        r = subprocess.run([str(tmp_path / "setup_forwarding_and_nat.sh"), f"--print-{what}"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout

    routing = tmp_path / "mnh-ap-local-routing.sh"
    routing.write_text(render("routing-script"))
    routing.chmod(0o755)
    hook = tmp_path / "91-mnh-ap-local-routing"
    hook.write_text(render("dispatcher").replace(
        "/usr/local/sbin/mnh-ap-local-routing.sh", str(routing)))
    hook.chmod(0o755)
    return {"routing": routing, "hook": hook}


# The Pi as it was: eth0 on 10.20.0.0/24 with a gateway, the AP bridge, and
# wg0 carrying a route for the home LAN (10.0.0.0/24) - the colliding peer LAN.
TOPOLOGY = """
set -euo pipefail
ip link set lo up
ip link add eth0 type dummy
ip addr add 10.20.0.230/24 dev eth0
ip link set eth0 up
ip route add default via 10.20.0.1 dev eth0
ip link add br-ap type dummy
ip addr add 10.2.73.1/24 dev br-ap
ip link set br-ap up
ip link add wg0 type dummy
ip addr add 10.73.73.3/32 dev wg0
ip link set wg0 up
ip route add 10.0.0.0/24 dev wg0
"""

HOME_LAN = "10.0.0.182"  # a host on the LAN that wg0 also claims


def netns(script: str, tools: dict[str, Path]) -> str:
    """Runs `script` (after the topology) in a fresh network namespace and
    returns its output. $ROUTING and $HOOK are the two scripts."""
    body = (f"ROUTING={tools['routing']}\nHOOK={tools['hook']}\n"
            + TOPOLOGY + textwrap.dedent(script))
    r = subprocess.run(["unshare", "-rn", "bash", "-c", body],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    return r.stdout


def lines(output: str, key: str) -> str:
    """The text after `key:` on its line (the scripts below print `key: value`)."""
    for line in output.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no {key!r} in:\n{output}")


class TestFilledAtBoot:
    def test_each_interface_gets_a_default_route_through_its_own_gateway(self, tools):
        out = netns("""
            "$ROUTING"
            echo "t101: $(ip route show table 101)"
            echo "t102: $(ip route show table 102)"
        """, tools)
        assert lines(out, "t101") == "default via 10.20.0.1 dev eth0"
        assert lines(out, "t102") == "default dev br-ap scope link"

    def test_replies_leave_the_way_the_connection_came_in_even_where_wg0_claims_the_lan(self, tools):
        out = netns(f"""
            "$ROUTING"
            echo "eth0: $(ip route get {HOME_LAN} mark 0x1 | head -1)"
            echo "bridge: $(ip route get {HOME_LAN} mark 0x2 | head -1)"
            echo "other: $(ip route get {HOME_LAN} | head -1)"
        """, tools)
        assert "dev eth0" in lines(out, "eth0")
        assert "dev br-ap" in lines(out, "bridge")
        # Everything else still prefers the tunnel, as documented: only
        # connections into the Pi itself are protected.
        assert "dev wg0" in lines(out, "other")

    def test_running_it_again_neither_doubles_the_rules_nor_empties_the_table(self, tools):
        out = netns("""
            for _ in 1 2 3; do "$ROUTING"; done
            echo "rules1: $(ip rule show | grep -c 'fwmark 0x1 lookup 101')"
            echo "rules2: $(ip rule show | grep -c 'fwmark 0x2 lookup 102')"
            echo "t101: $(ip route show table 101)"
        """, tools)
        assert lines(out, "rules1") == "1"
        assert lines(out, "rules2") == "1"
        assert lines(out, "t101") == "default via 10.20.0.1 dev eth0"


class TestSurvivesTheNetworkChanging:
    def test_a_link_flap_empties_the_table_and_the_hook_fills_it_again(self, tools):
        """The incident. Without the hook the second assertion is where it broke."""
        out = netns(f"""
            "$ROUTING"
            ip link set eth0 down; ip link set eth0 up
            ip route add default via 10.20.0.1 dev eth0        # what DHCP puts back
            echo "t101_after_flap: [$(ip route show table 101)]"
            echo "reply_after_flap: $(ip route get {HOME_LAN} mark 0x1 | head -1)"
            "$HOOK" eth0 up
            echo "t101_after_hook: $(ip route show table 101)"
            echo "reply_after_hook: $(ip route get {HOME_LAN} mark 0x1 | head -1)"
        """, tools)
        assert lines(out, "t101_after_flap") == "[]", "the flap should have emptied the table"
        assert "dev wg0" in lines(out, "reply_after_flap"), "and sent replies into the tunnel"
        assert lines(out, "t101_after_hook") == "default via 10.20.0.1 dev eth0"
        assert "dev eth0" in lines(out, "reply_after_hook")

    def test_a_new_gateway_replaces_the_old_one(self, tools):
        out = netns("""
            "$ROUTING"
            ip route replace default via 10.20.0.2 dev eth0     # a new lease, a new gateway
            "$HOOK" eth0 dhcp4-change
            echo "t101: $(ip route show table 101)"
        """, tools)
        assert lines(out, "t101") == "default via 10.20.0.2 dev eth0"

    def test_an_interface_that_has_gone_is_dropped_without_disturbing_the_others(self, tools):
        out = netns("""
            "$ROUTING"
            ip link del br-ap
            "$HOOK" br-ap down
            echo "t102: [$(ip route show table 102)]"
            echo "rule2: $(ip rule show | grep -c 'fwmark 0x2 lookup 102')"
            echo "t101: $(ip route show table 101)"
        """, tools)
        assert lines(out, "t102") == "[]"
        assert lines(out, "rule2") == "0"
        assert lines(out, "t101") == "default via 10.20.0.1 dev eth0"

    def test_the_interface_coming_back_gets_its_table_again(self, tools):
        out = netns("""
            ip link del br-ap
            "$ROUTING"
            echo "before: [$(ip route show table 102)]"
            ip link add br-ap type dummy; ip addr add 10.2.73.1/24 dev br-ap; ip link set br-ap up
            "$HOOK" br-ap up
            echo "after: $(ip route show table 102)"
        """, tools)
        assert lines(out, "before") == "[]"
        assert lines(out, "after") == "default dev br-ap scope link"

    def test_an_interface_that_is_down_does_not_fail_the_hook_or_leave_a_stale_route(self, tools):
        out = netns("""
            "$ROUTING"
            ip link set eth0 down
            "$HOOK" eth0 down
            echo "status: ok"
            echo "t101: [$(ip route show table 101)]"
        """, tools)
        assert lines(out, "status") == "ok"
        assert lines(out, "t101") == "[]"


class TestTheHook:
    def test_it_waits_for_the_event_that_follows_configuration(self, tools):
        out = netns("""
            "$ROUTING"
            ip route flush table 101
            "$HOOK" eth0 pre-up
            "$HOOK" eth0 pre-down
            "$HOOK" eth0 hostname
            echo "after_pre_events: [$(ip route show table 101)]"
            "$HOOK" eth0 up
            echo "after_up: $(ip route show table 101)"
        """, tools)
        assert lines(out, "after_pre_events") == "[]"
        assert lines(out, "after_up") == "default via 10.20.0.1 dev eth0"

    def test_the_setup_script_installs_it_executable_where_networkmanager_looks(self):
        text = SETUP.read_text()
        path = "/etc/NetworkManager/dispatcher.d/91-mnh-ap-local-routing"
        assert f"render_dispatcher | tee {path}" in text
        assert f"chmod 755 {path}" in text

    def test_it_runs_after_the_wg0_precedence_hook_which_is_named_90(self):
        assert "dispatcher.d/90-mnh-ap-wg-route-precedence" in (SCRIPTS / "setup_wireguard.sh").read_text()
        assert "dispatcher.d/91-mnh-ap-local-routing" in SETUP.read_text()

    def test_it_runs_the_script_the_setup_installs(self):
        text = SETUP.read_text()
        assert "render_routing_script | tee /usr/local/sbin/mnh-ap-local-routing.sh" in text
        assert "exec /usr/local/sbin/mnh-ap-local-routing.sh" in text
