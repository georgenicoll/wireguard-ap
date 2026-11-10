"""The metrics collector as deployed on the Pi: running, unprivileged, its
socket closed to everyone but the web app's group, and pinned to the right
release. Tier: smoke.
"""
import re

import pytest

from support import REPO

SOCKET = "/run/simple-metrics/simple-metrics.sock"
SERVICE = "mnh-ap-metrics.service"
WEB_SOCKET = "/run/simple-metrics-web/smweb.sock"
WEB_SERVICE = "mnh-ap-metrics-web.service"


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

    def test_smq_is_in_the_deploy_users_home_and_is_the_pinned_release(self, pi):
        if tuple(int(n) for n in pinned_version().split(".")) < (0, 3, 0):
            pytest.skip("the pinned release predates smq")
        assert pi.out("stat -c '%a %U' ~/smq").strip() == f"755 {pi.out('id -un').strip()}"
        assert pi.out("~/smq --version").strip() == f"smq {pinned_version()}"

    def test_smq_works_for_the_deploy_user_with_no_password(self, pi):
        if tuple(int(n) for n in pinned_version().split(".")) < (0, 3, 0):
            pytest.skip("the pinned release predates smq")
        r = pi.ssh("smq info")
        assert r.returncode == 0, r.stderr
        assert "metrics      15" in r.stdout, r.stdout
        assert "cpu_percent" in pi.out("smq metrics")
        # Through the sudo rule, so a plain (unwrapped) ~/smq is still refused.
        assert pi.ssh(f"~/smq --socket {SOCKET} info").returncode != 0

    def test_the_sudo_rule_covers_smq_and_nothing_else(self, pi):
        assert pi.out("stat -c '%a %U %G' /usr/local/libexec/smq /usr/local/bin/smq").split("\n")[:2] == \
            ["755 root root", "755 root root"]
        # No password is wanted for smq, but is for anything else with that
        # group. `sudo -K` first: a password typed recently in any session
        # would otherwise still be remembered (the Pi's timestamps are global).
        assert pi.ssh("sudo -K; sudo -n -g mnh-web /bin/true").returncode != 0
        assert pi.ssh("sudo -K; sudo -n -g mnh-web /usr/local/libexec/smq --version").returncode == 0
        assert pi.ssh("sudo -K; smq --version").returncode == 0

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

    def test_the_history_is_kept_in_a_private_state_directory(self, pi):
        if tuple(int(n) for n in pinned_version().split(".")) < (0, 4, 0):
            pytest.skip("the pinned release predates the history file")
        got = pi.out("stat -c '%a %U %G' /var/lib/simple-metrics").strip()
        assert got == "700 mnh-metrics mnh-web", got
        assert "--state-dir /var/lib/simple-metrics" in pi.out(f"systemctl show -p ExecStart {SERVICE}")

    def test_it_says_what_it_loaded_at_startup(self, pi):
        if tuple(int(n) for n in pinned_version().split(".")) < (0, 4, 0):
            pytest.skip("the pinned release predates the history file")
        log = pi.out(f"journalctl -u {SERVICE} --no-pager -o cat")
        assert "history:" in log, log

    def test_the_whole_store_is_resident_from_the_start(self, pi):
        if tuple(int(n) for n in pinned_version().split(".")) < (0, 4, 0):
            pytest.skip("the pinned release predates pre-touching the store")
        rss_kib = int(pi.out("ps -o rss= -C simple-metrics").split()[0])
        assert rss_kib > 14 * 1024, f"only {rss_kib // 1024} MiB resident"

    def test_the_deploy_user_has_no_access_to_the_socket(self, pi):
        # The data is reachable only through the web app, and that needs the
        # login: there is no way in from the deploy user's own shell.
        r = pi.ssh(f"test -r {SOCKET} && test -w {SOCKET}")
        assert r.returncode != 0


def has_smweb() -> bool:
    return tuple(int(n) for n in pinned_version().split(".")) >= (0, 5, 0)


@pytest.mark.smoke
class TestPiMetricsWeb:
    """smweb, which writes the Metrics page's chart: running unprivileged, with
    a Unix socket the web app can reach and nothing else can, and no network
    listener of its own."""

    @pytest.fixture(autouse=True)
    def _needs_smweb(self):
        if not has_smweb():
            pytest.skip("the pinned release predates smweb")

    def test_the_service_is_enabled_and_running(self, pi):
        assert pi.out(f"systemctl is-enabled {WEB_SERVICE}").strip() == "enabled"
        assert pi.out(f"systemctl is-active {WEB_SERVICE}").strip() == "active"

    def test_it_runs_as_the_collectors_user_not_root_and_not_the_web_user(self, pi):
        assert pi.out("ps -o user= -C smweb").split() == ["mnh-metrics"]

    def test_it_is_the_pinned_release_and_root_owned(self, pi):
        assert pi.out("/usr/local/bin/smweb --version").strip() == f"smweb {pinned_version()}"
        assert pi.out("stat -c '%a %U %G' /usr/local/bin/smweb").strip() == "755 root root"

    def test_its_socket_is_closed_to_everyone_but_the_owner_and_web_group(self, pi):
        got = pi.out("stat -c '%a %U %G' /run/simple-metrics-web").strip()
        assert got == "750 mnh-metrics mnh-web", got
        assert WEB_SOCKET in pi.out("ss -xlH")

    def test_other_accounts_cannot_connect(self, pi):
        assert pi.out("id -Gn").split().count("mnh-web") == 0
        r = pi.ssh(f"python3 -c \"import socket; s = socket.socket(socket.AF_UNIX); s.connect('{WEB_SOCKET}')\"")
        assert r.returncode != 0 and "Permission denied" in r.stderr, r.stderr

    def test_it_has_no_network_listener(self, pi):
        listening = pi.out("ss -ltnp 2>/dev/null || true")
        assert "smweb" not in listening, "it should only be reachable through its socket"

    def test_it_is_locked_down(self, pi):
        props = dict(line.split("=", 1) for line in
                     pi.out(f"systemctl show {WEB_SERVICE} -p NoNewPrivileges -p ProtectSystem "
                            "-p CapabilityBoundingSet -p RestrictAddressFamilies "
                            "-p MemoryDenyWriteExecute -p PrivateDevices -p ProtectProc").splitlines() if "=" in line)
        assert props["NoNewPrivileges"] == "yes" and props["ProtectSystem"] == "strict"
        assert props["CapabilityBoundingSet"] == "" and props["PrivateDevices"] == "yes"
        assert props["MemoryDenyWriteExecute"] == "yes" and props["RestrictAddressFamilies"] == "AF_UNIX"
        assert props["ProtectProc"] == "invisible"

    def test_it_started_cleanly_and_says_where_it_listens(self, pi):
        log = pi.out(f"journalctl -u {WEB_SERVICE} -b --no-pager -o cat")
        assert f"on {WEB_SOCKET}" in log, log

    def test_restarting_the_collector_does_not_take_its_socket_away(self, pi):
        """Separate runtime directories: systemd removes a service's when it
        stops. (Checked by looking at the units, not by restarting anything.)"""
        web = pi.out(f"systemctl show {WEB_SERVICE} -p RuntimeDirectory --value").strip()
        collector = pi.out(f"systemctl show {SERVICE} -p RuntimeDirectory --value").strip()
        assert web and collector and web != collector


def metric_names(html: str) -> list[str]:
    select = re.search(r'<select id="metric-select".*?</select>', html, re.S).group(0)
    return re.findall(r'<option value="([^"]+)"', select)


@pytest.mark.smoke
class TestPiMetricsPage:
    """The Metrics page and its data, through the deployed web app."""

    def test_the_page_needs_a_login(self, pi):
        from support import Web
        anonymous = Web(pi.base_url, "not-the-passkey")
        for path in ("/metrics", "/metrics/data?metric=cpu_percent&range=1h"):
            r = anonymous.get(path)
            assert r.status_code in (303, 401) or r.url.path == "/login", path

    def test_the_page_lists_every_metric_the_collector_records(self, pi_web):
        r = pi_web.get("/metrics")
        assert r.status_code == 200
        assert "No metrics available" not in r.text, "the web app cannot reach the collector"
        names = metric_names(r.text)
        assert len(names) == 15, names
        assert {"cpu_percent", "load1", "mem_used_bytes", "swap_used_bytes",
                "cpu_temp_celsius"} <= set(names)

    def test_every_metric_returns_a_series_for_every_range(self, pi_web):
        names = metric_names(pi_web.get("/metrics").text)
        for name in names:
            for span in ("1h", "7d"):
                r = pi_web.get(f"/metrics/data?metric={name}&range={span}")
                assert r.status_code == 200, (name, span, r.text)
                data = r.json()
                n = len(data["timestamps"])
                assert n > 0 and n <= 600, (name, span, n)
                assert len(data["avg"]) == len(data["min"]) == len(data["max"]) == n

    def test_the_data_is_recent(self, pi_web):
        data = pi_web.get("/metrics/data?metric=cpu_percent&range=1h").json()
        newest = data["timestamps"][-1]
        assert data["now"] - newest < 60_000, "the collector has stopped recording"

    def test_bad_requests_are_refused(self, pi_web):
        assert pi_web.get("/metrics/data?metric=nope&range=1h").status_code == 404
        assert pi_web.get("/metrics/data?metric=cpu_percent&range=1y").status_code == 400
        assert pi_web.get("/metrics/data?metric=../etc&range=1h").status_code == 400

    def test_the_chart_is_served_from_the_pi_through_the_web_app(self, pi_web):
        if not has_smweb():
            pytest.skip("the pinned release predates smweb")
        page = pi_web.get("/metrics").text
        assert "/metrics/assets/metrics.js" in page and "/metrics/assets/uPlot.iife.min.js" in page
        assert 'data-url="/metrics/data"' in page
        assert "http://" not in page and "https://" not in page, "nothing from the internet"
        for name, kind in (("metrics.js", "javascript"), ("uPlot.iife.min.js", "javascript"),
                           ("metrics.css", "css"), ("uPlot.min.css", "css")):
            r = pi_web.get(f"/metrics/assets/{name}")
            assert r.status_code == 200 and kind in r.headers["content-type"], name

    def test_the_assets_need_a_login_too(self, pi):
        if not has_smweb():
            pytest.skip("the pinned release predates smweb")
        from support import Web
        r = Web(pi.base_url, "not-the-passkey").get("/metrics/assets/metrics.js")
        assert r.status_code in (303, 401) or r.url.path == "/login"
