"""The deployed web app over HTTPS: login and session security, every
read-only page. Tier: smoke.
"""
import re

import httpx
import pytest

from support import Web


@pytest.mark.smoke
class TestPiWeb:
    def test_health_endpoint_is_public(self, pi):
        r = httpx.get(pi.base_url + "/healthz", verify=False, timeout=10)
        assert r.status_code == 200 and r.text.strip() == "ok"

    @pytest.mark.parametrize("path", ["/", "/diagnostics", "/manage", "/ap-details",
                                       "/wireguard-details"])
    def test_pages_require_login(self, pi, path):
        r = httpx.get(pi.base_url + path, verify=False, follow_redirects=False, timeout=10)
        assert r.status_code == 303 and r.headers["location"] == "/login"

    def test_running_a_diagnostic_requires_login(self, pi):
        r = httpx.post(pi.base_url + "/run/diagnostics", data={"command": "ip-addr-list"},
                       verify=False, follow_redirects=False, timeout=10)
        assert r.status_code == 303

    def test_wrong_password_is_refused(self, pi):
        r = httpx.post(pi.base_url + "/login", data={"password": "definitely-wrong"},
                       verify=False, follow_redirects=False, timeout=10)
        assert r.status_code == 401

    def test_session_cookie_is_secure_httponly_and_samesite(self, pi):
        cookie = Web(pi.base_url, pi.psk).login().headers.get("set-cookie", "").lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie

    def test_a_login_lasts_an_hour_and_using_the_site_restarts_it(self, pi):
        web = Web(pi.base_url, pi.psk)
        cookie = web.login().headers.get("set-cookie", "").lower()
        assert "max-age=3600" in cookie, cookie.split(";")[1:3]
        # An ordinary page load re-issues the cookie (sliding expiry).
        assert "max-age=3600" in web.get("/").headers.get("set-cookie", "").lower()

    def test_the_login_cookie_holds_only_the_flag_and_the_login_time(self, pi):
        import base64
        import json

        cookie = Web(pi.base_url, pi.psk).login().headers["set-cookie"]
        value = re.search(r"session=([^;]+)", cookie).group(1).rsplit(".", 2)[0]
        payload = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
        assert set(payload) == {"authenticated", "login_at"}
        # Compared against the Pi's own clock, not this machine's.
        pi_now = int(pi.out("date +%s").strip())
        assert abs(pi_now - payload["login_at"]) < 60

    def test_home_shows_real_values(self, pi_web):
        text = pi_web.get("/").text
        assert "<title>mnh-ap</title>" in text
        assert "not found" not in text and ">unknown<" not in text.lower()

    def test_ap_details_page_has_no_permission_errors(self, pi_web):
        text = pi_web.get("/ap-details").text
        assert "== Devices ==" in text
        for bad in ("password is required", "not allowed", "Permission denied", "exit code"):
            assert bad not in text

    def test_wireguard_details_page(self, pi_web):
        text = pi_web.get("/wireguard-details").text
        assert "interface: wg0" in text and "latest handshake" in text

    def test_diagnostics_page_lists_every_command(self, pi_web):
        text = pi_web.get("/diagnostics").text
        for name in ("ping", "dig", "wg-status", "logs", "service-status"):
            assert f'value="{name}"' in text
        assert "mnh-ap - Diagnostics" in text

    def test_diagnostics_page_shows_nothing_until_one_is_chosen(self, pi_web):
        text = pi_web.get("/diagnostics").text
        cards = re.findall(r'<article data-command="[^"]*"([^>]*)>', text)
        assert cards and all("hidden" in attrs for attrs in cards)
