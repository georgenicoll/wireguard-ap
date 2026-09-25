"""Every diagnostic command run through the deployed web app, and refusal of
bad requests. Tier: smoke.
"""
import re
import time

import pytest

from support import PiConfig


def gateway(pi: PiConfig) -> str:
    return pi.out("ip route show default").split("via")[1].split()[0]


@pytest.mark.smoke
class TestPiDiagnostics:
    def test_wg_status_goes_through_sudo(self, pi_web):
        r = pi_web.run("wg-status")
        assert "interface: wg0" in r.out and "exit code 0" in r.out

    @pytest.mark.parametrize("unit", ["mnh-ap-nat", "mnh-ap-webapp", "mnh-ap-local-routing"])
    def test_service_status(self, pi_web, unit):
        r = pi_web.run("service-status", unit)
        assert "Active: active" in r.out and "exit code 0" in r.out

    def test_logs_are_readable_by_the_web_user(self, pi_web):
        r = pi_web.run("logs", "mnh-ap-webapp", "500")
        assert re.search(r"^\w{3} +\d+ \d\d:\d\d:\d\d ", r.out, re.M), r.out[:200]
        assert "No entries" not in r.out and "Permission" not in r.out

    def test_route_commands(self, pi, pi_web):
        assert "fwmark" in pi_web.run("ip-route-list").out
        gw = gateway(pi)
        r = pi_web.run("ip-route-get", gw)
        assert gw in r.out and "exit code 0" in r.out

    def test_addresses_and_clients(self, pi_web):
        assert "wg0" in pi_web.run("ip-addr-list").out
        r = pi_web.run("ap-clients")
        assert "== Devices ==" in r.out and "exit code 0" in r.out

    def test_dig_and_traceroute_are_installed_and_work(self, pi, pi_web):
        r = pi_web.run("dig", "example.com", "", "")
        assert "not installed" not in r.out and "ANSWER" in r.out
        r = pi_web.run("dig", "example.com", "", "MX")
        assert "IN" in r.out and "MX" in r.out
        assert "traceroute to" in pi_web.run("traceroute", gateway(pi), timeout=60).out

    @pytest.mark.parametrize("command,params", [
        ("nope", []), ("ping", []), ("ping", ["127.0.0.1", "x"]), ("dig", ["", "", ""]),
        ("logs", ["dnsmasq", "7"]), ("logs", ["ssh", "50"]), ("logs", ["dnsmasq,wg-quick@wg0", "50"]),
    ])
    def test_the_app_rejects_invalid_requests(self, pi_web, command, params):
        assert pi_web.run(command, *params).status == 400

    def test_an_injection_attempt_runs_nothing(self, pi, pi_web):
        marker = "/tmp/wga-it-pwned"
        pi.ssh(f"rm -f {marker}")
        for value in (f"127.0.0.1;touch {marker}", f"$(touch {marker})", "-c1", "--help"):
            r = pi_web.run("ip-route-get", value)
            assert "invalid host" in r.out and "exit code 2" in r.out, r.out
        assert pi.ssh(f"test -e {marker}").returncode != 0

    def test_ping_runs_until_the_stream_is_dropped_then_stops(self, pi, pi_web):
        check = "pgrep -f 'ping -W 3 12[7].0.0.1' >/dev/null"
        r, job = pi_web.start("ping", "127.0.0.1")
        assert job
        running = False
        with pi_web.client.stream("GET", f"/run/stream/{job}", timeout=30) as s:
            replies = 0
            for line in s.iter_lines():
                replies += "bytes from" in line
                if replies >= 2:
                    # Checked while the stream is still open: leaving this
                    # loop drops the connection, which is what stops the ping.
                    running = pi.ssh(check).returncode == 0
                    break
        assert running, "ping should be running while its stream is open"
        deadline = time.time() + 10
        while time.time() < deadline and pi.ssh(check).returncode == 0:
            time.sleep(1)
        assert pi.ssh(check).returncode != 0, "ping should stop once the stream closes"
