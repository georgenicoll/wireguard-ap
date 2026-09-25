"""The login form's responses, against a throwaway local server. Tier: local."""
import pytest

from support import LOCAL_PSK, Web


@pytest.mark.local
class TestLogin:
    def _post(self, local_server, **form):
        return Web(local_server.base_url, LOCAL_PSK).client.post("/login", data=form)

    @pytest.mark.parametrize("form", [{"password": ""}, {}])
    def test_a_blank_passkey_asks_for_one(self, local_server, form):
        r = self._post(local_server, **form)
        assert r.status_code == 400
        assert "Enter PassKey" in r.text and 'aria-invalid="true"' in r.text
        assert "set-cookie" not in r.headers

    def test_a_wrong_passkey_is_refused(self, local_server):
        r = self._post(local_server, password="definitely-wrong")
        assert r.status_code == 401
        assert "Incorrect PassKey" in r.text and "set-cookie" not in r.headers

    def test_the_right_passkey_logs_in(self, local_server):
        assert self._post(local_server, password=LOCAL_PSK).status_code == 303
