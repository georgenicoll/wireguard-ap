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
