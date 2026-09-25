"""The metrics collector as deployed on the Pi: running, unprivileged, its
socket closed to everyone but the web app's group, and pinned to the right
release. Tier: smoke.
"""
import re

import pytest

from support import REPO

SOCKET = "/run/simple-metrics/simple-metrics.sock"
SERVICE = "mnh-ap-metrics.service"


def pinned_version() -> str:
    text = (REPO / "simple-metrics.pin").read_text()
    return re.search(r"^SIMPLE_METRICS_VERSION=v(\S+)$", text, re.M).group(1)


@pytest.mark.smoke
class TestPiMetrics:
    def test_the_service_is_enabled_and_running(self, pi):
        assert pi.out(f"systemctl is-enabled {SERVICE}").strip() == "enabled"
        assert pi.out(f"systemctl is-active {SERVICE}").strip() == "active"

    def test_it_runs_as_its_own_user_not_root_and_not_the_web_user(self, pi):
        users = pi.out("ps -o user= -C simple-metrics").split()
        assert users == ["mnh-metrics"], users

    def test_it_is_the_pinned_release(self, pi):
        assert pi.out("/usr/local/bin/simple-metrics --version").strip() == \
            f"simple-metrics {pinned_version()}"

    def test_the_binary_is_root_owned_and_cannot_be_changed_by_the_services(self, pi):
        assert pi.out("stat -c '%a %U %G' /usr/local/bin/simple-metrics").strip() == "755 root root"

    def test_the_socket_directory_is_closed_to_everyone_but_the_owner_and_web_group(self, pi):
        got = pi.out("stat -c '%a %U %G' /run/simple-metrics").strip()
        assert got == "750 mnh-metrics mnh-web", got

    def test_the_collector_is_listening_on_its_socket(self, pi):
        # Only listed here, not looked at: the deploy user can't enter the
        # directory (that is the point), so it can't stat the socket. That the
        # web group *can* use it is proved by the web app reading through it.
        assert SOCKET in pi.out("ss -xlH")

    def test_other_accounts_cannot_connect(self, pi):
        # The deploy user isn't in the web group, so this is what any other
        # account on the Pi would get.
        assert pi.out("id -Gn").split().count("mnh-web") == 0
        r = pi.ssh("python3 -c \"import socket; s = socket.socket(socket.AF_UNIX); "
                   f"s.connect('{SOCKET}')\"")
        assert r.returncode != 0
        assert "Permission denied" in r.stderr, r.stderr

    def test_it_started_cleanly_and_says_where_it_listens(self, pi):
        log = pi.out(f"journalctl -u {SERVICE} -b --no-pager -o cat")
        assert f"listening on {SOCKET}" in log
        assert "sampling every 5s" in log and "keeping 120960 records" in log

    def test_it_is_locked_down(self, pi):
        props = dict(line.split("=", 1) for line in
                     pi.out(f"systemctl show {SERVICE} -p NoNewPrivileges -p ProtectSystem "
                            "-p CapabilityBoundingSet -p RestrictAddressFamilies "
                            "-p MemoryDenyWriteExecute -p PrivateDevices").splitlines() if "=" in line)
        assert props["NoNewPrivileges"] == "yes"
        assert props["ProtectSystem"] == "strict"
        assert props["CapabilityBoundingSet"] == ""
        assert props["MemoryDenyWriteExecute"] == "yes"
        assert props["PrivateDevices"] == "yes"
        assert props["RestrictAddressFamilies"] == "AF_UNIX"

    def test_its_memory_use_stays_small(self, pi):
        # About 15 MB when its history is full, plus the runtime. A generous
        # ceiling: this is to catch a leak or a runaway, not to measure.
        rss_kib = int(pi.out("ps -o rss= -C simple-metrics").split()[0])
        assert rss_kib < 64 * 1024, f"{rss_kib // 1024} MiB"

    def test_the_deploy_user_has_no_access_to_the_socket(self, pi):
        # The data is reachable only through the web app, and that needs the
        # login: there is no way in from the deploy user's own shell.
        r = pi.ssh(f"test -r {SOCKET} && test -w {SOCKET}")
        assert r.returncode != 0
