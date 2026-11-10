"""The deployed Pi's own state over SSH: services, routing, permissions,
leftovers. Tier: smoke.
"""


import pytest

from support import DEPLOYED_SCRIPTS, expected_units


@pytest.mark.smoke
class TestPiState:
    def test_services_are_enabled_and_active(self, pi):
        bad = []
        for unit in expected_units(pi):
            enabled = pi.out(f"systemctl is-enabled {unit}").strip()
            active = pi.out(f"systemctl is-active {unit}").strip()
            if enabled != "enabled" or active != "active":
                bad.append(f"{unit}: enabled={enabled} active={active}")
        assert not bad, bad

    def test_no_failed_units(self, pi):
        assert pi.out("systemctl --failed --no-legend").strip() == ""

    def test_web_app_runs_as_the_unprivileged_user_on_443(self, pi):
        assert "mnh-web" in pi.out("ps -o user= -C python").split()
        assert ":443 " in pi.out("ss -ltn")
        assert pi.out("systemctl show mnh-ap-webapp -p User --value").strip() == "mnh-web"

    def test_routing_protection_is_in_place(self, pi):
        rules = pi.out("ip rule")
        assert "fwmark 0x1 lookup 101" in rules and "fwmark 0x2 lookup 102" in rules
        assert "wg0" in pi.out("ip -br link")

    def test_the_reply_routing_tables_are_not_empty(self, pi):
        """The rules above can be there with nothing behind them: the tables'
        routes go when an interface is reconfigured, and replies to anything
        arriving on eth0 then fall through to wg0 (SSH and the web app, from
        the network the Pi is on, time out while everything else looks fine)."""
        if " UP " not in pi.out("ip -br link show eth0 2>&1 || true"):
            pytest.skip("eth0 is down: its table is meant to be empty then")
        table = pi.out("ip route show table 101").strip()
        gateway = pi.out("ip route show default dev eth0 | awk '{print $3; exit}'").strip()
        want = f"default via {gateway} dev eth0" if gateway else "default dev eth0 scope link"
        assert table == want, f"table 101 is {table!r}, expected {want!r}"
        assert "dev " in pi.out("ip route show table 102"), "the AP bridge's table is empty"

    def test_replies_to_connections_arriving_on_eth0_leave_through_eth0(self, pi):
        if " UP " not in pi.out("ip -br link show eth0 2>&1 || true"):
            pytest.skip("eth0 is down")
        for destination in ("1.1.1.1", "10.0.0.1"):
            route = pi.out(f"ip route get {destination} mark 0x1 | head -1")
            assert "dev eth0" in route, f"a reply to {destination} would go: {route.strip()}"

    def test_the_network_event_hook_that_refills_them_is_installed(self, pi):
        mode = pi.out("stat -c '%a %U' /etc/NetworkManager/dispatcher.d/91-mnh-ap-local-routing").strip()
        assert mode == "755 root"

    def test_state_directory_is_private_to_the_web_user(self, pi):
        assert pi.out("stat -c '%a %U:%G' /var/lib/mnh-ap").strip() == "700 mnh-web:mnh-web"
        # ...and the deploy user genuinely can't see inside it.
        assert pi.ssh("ls /var/lib/mnh-ap").returncode != 0

    def test_web_user_can_only_traverse_the_home_directory(self, pi):
        acl = pi.out(f"getfacl -p /home/{pi.user}")
        assert "user:mnh-web:--x" in acl and "other::---" in acl and "group::---" in acl

    @pytest.mark.parametrize("name", DEPLOYED_SCRIPTS)
    def test_scripts_are_owned_by_the_deploy_user_and_only_writable_by_them(self, pi, name):
        got = pi.out(f"stat -c '%a %U' /home/{pi.user}/{name}").strip()
        assert got == f"755 {pi.user}", f"{name}: {got!r} (expected 755 {pi.user})"

    def test_web_user_cannot_write_where_root_runs_code_from(self, pi):
        r = pi.ssh(f"find /home/{pi.user} -maxdepth 2 -writable -not -user {pi.user} 2>/dev/null")
        assert r.stdout.strip() == ""

    @pytest.mark.parametrize("path", [
        "webapp/dev-cert.pem", "webapp/dev-key.pem", "webapp/cert.pem", "webapp/key.pem",
        "webapp/.session_secret", "webapp/__pycache__", ".wg0.conf",
    ])
    def test_secrets_and_dev_files_are_not_left_in_the_deployed_tree(self, pi, path):
        assert pi.ssh(f"test -e /home/{pi.user}/{path}").returncode != 0, path

    def test_no_files_left_under_the_old_mnet_name(self, pi):
        # The NetworkManager profile named after the upstream Wi-Fi network
        # ("mnet") is a real SSID, not this project's old name.
        found = pi.out("find /etc /usr/local /var/lib -iname '*mnet*' "
                       "-not -path '/etc/NetworkManager/system-connections/*' 2>/dev/null")
        assert found.strip() == "", found

    def test_dnsmasq_has_no_stale_project_config(self, pi):
        names = pi.out("ls /etc/dnsmasq.d").split()
        assert [n for n in names if n != "README" and n != "mnh-ap.conf"] == []
