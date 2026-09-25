"""Session cookie lifetime and expiry, against the local server. Tier: local.
"""
import re
import time

import httpx
import pytest

from support import Web, LOCAL_PSK


@pytest.mark.local
class TestSessionLifetime:
    def _cookie(self, secret: str, age_seconds: int, login_age: int | None = ...,
                **extra) -> str:
        """A correctly signed session cookie, as the server would have issued
        it age_seconds ago, for a login that happened login_age seconds ago
        (default: at the same moment; None = no login time at all)."""
        import base64
        import json
        from itsdangerous import TimestampSigner

        class Aged(TimestampSigner):
            def get_timestamp(self) -> int:
                return int(time.time()) - age_seconds

        session = {"authenticated": True, **extra}
        if login_age is ...:
            login_age = age_seconds
        if login_age is not None:
            session["login_at"] = int(time.time()) - login_age
        data = base64.b64encode(json.dumps(session).encode())
        return Aged(secret).sign(data).decode()

    @staticmethod
    def _payload(set_cookie: str) -> dict:
        import base64
        import json

        value = re.search(r"session=([^;]+)", set_cookie).group(1)
        body = value.rsplit(".", 2)[0]
        return json.loads(base64.b64decode(body + "=" * (-len(body) % 4)))

    def _get(self, server, cookie: str) -> httpx.Response:
        return httpx.get(server.base_url + "/diagnostics", verify=False, timeout=10,
                         follow_redirects=False, cookies={"session": cookie})

    def test_login_cookie_lasts_an_hour(self, local_server):
        r = Web(local_server.base_url, LOCAL_PSK).login()
        assert "max-age=3600" in r.headers["set-cookie"].lower()

    def test_using_the_site_restarts_the_hour(self, local_server):
        """Sliding expiry: an authenticated page load hands back a fresh
        cookie - checked with one that is 50 minutes old."""
        import base64

        secret = (local_server.state_dir / ".session_secret").read_text().strip()
        old = self._cookie(secret, 3000)
        r = httpx.get(local_server.base_url + "/diagnostics", verify=False, timeout=10,
                      follow_redirects=False, cookies={"session": old})
        assert r.status_code == 200
        cookie = r.headers.get("set-cookie", "")
        assert "max-age=3600" in cookie.lower(), "no refreshed cookie was issued"
        stamp = re.search(r"session=[^;]+\.([^.;]+)\.[^.;]+;", cookie).group(1)
        issued = int.from_bytes(base64.urlsafe_b64decode(stamp + "=" * (-len(stamp) % 4)), "big")
        assert abs(time.time() - issued) < 10, "the refreshed cookie should be stamped now"

    def test_pages_that_need_no_login_do_not_hand_out_a_session(self, local_server):
        r = httpx.get(local_server.base_url + "/healthz", verify=False, timeout=10)
        assert "set-cookie" not in r.headers
        r = httpx.get(local_server.base_url + "/login", verify=False, timeout=10)
        assert "set-cookie" not in r.headers

    # --- the absolute cap (SESSION_ABSOLUTE_LIMIT_SECONDS in app.py) ----------
    HOUR = 3600
    CAP = 4 * HOUR

    def test_login_records_when_it_happened(self, local_server):
        before = int(time.time())
        payload = self._payload(Web(local_server.base_url, LOCAL_PSK).login().headers["set-cookie"])
        assert payload["authenticated"] is True
        assert before <= payload["login_at"] <= time.time() + 1

    def test_a_login_older_than_four_hours_is_refused_however_fresh_the_cookie(self, local_server):
        secret = (local_server.state_dir / ".session_secret").read_text().strip()
        # A cookie issued a second ago (so still 'active') for a login just past the cap.
        r = self._get(local_server, self._cookie(secret, 1, login_age=self.CAP + 60))
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert "expires=thu, 01 jan 1970" in r.headers["set-cookie"].lower()  # and it's cleared

    def test_a_login_just_inside_the_cap_still_works(self, local_server):
        secret = (local_server.state_dir / ".session_secret").read_text().strip()
        r = self._get(local_server, self._cookie(secret, 1, login_age=self.CAP - 120))
        assert r.status_code == 200

    def test_activity_does_not_extend_the_cap(self, local_server):
        """The sliding refresh restarts the idle hour but must carry the
        original login time through unchanged."""
        secret = (local_server.state_dir / ".session_secret").read_text().strip()
        login_age = self.CAP - 10 * 60
        r = self._get(local_server, self._cookie(secret, 60, login_age=login_age))
        assert r.status_code == 200
        payload = self._payload(r.headers["set-cookie"])
        assert abs((time.time() - payload["login_at"]) - login_age) < 10

    @pytest.mark.parametrize("why,kwargs", [
        ("no login time (a cookie from before the cap existed)", {"login_age": None}),
        ("a login time in the future (clock went backwards)", {"login_age": -3600}),
        ("a login time that isn't a number", {"login_age": None, "login_at": "yesterday"}),
    ])
    def test_a_cookie_without_a_usable_login_time_is_refused(self, local_server, why, kwargs):
        secret = (local_server.state_dir / ".session_secret").read_text().strip()
        r = self._get(local_server, self._cookie(secret, 60, **kwargs))
        assert r.status_code == 303, why

    def test_a_refreshed_cookie_still_expires_when_left_alone(self, local_server):
        secret = (local_server.state_dir / ".session_secret").read_text().strip()
        r = httpx.get(local_server.base_url + "/diagnostics", verify=False, timeout=10,
                      follow_redirects=False, cookies={"session": self._cookie(secret, 3700)})
        assert r.status_code == 303 and "set-cookie" not in r.headers  # expired: no refresh

    def test_the_server_refuses_a_cookie_older_than_that(self, local_server):
        secret = (local_server.state_dir / ".session_secret").read_text().strip()

        def status(age):
            r = httpx.get(local_server.base_url + "/diagnostics", verify=False, timeout=10,
                          follow_redirects=False,
                          cookies={"session": self._cookie(secret, age)})
            return r.status_code

        assert status(60) == 200          # a minute old: fine
        assert status(3500) == 200        # just inside the hour
        assert status(3700) == 303        # just outside: back to the login
        assert status(14 * 24 * 3600) == 303  # the old two-week lifetime is gone
