# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pytest",
#   "httpx",
#   "playwright",
# ]
# ///
"""Integration tests for wireguard-ap. Run them with ./wga test (see the
README's "Integration tests" section); this file is also a plain uv script:

    uv run tests/integration.py [tier ...] [pytest args ...]

Tiers (markers):
  local   no Pi needed: the diagnostics scripts' argument handling (incl.
          injection attempts) and the UI in a browser, against a throwaway
          copy of the web app on a spare port.
  smoke   the deployed Pi, over SSH and HTTPS: services, routing, file
          permissions, leftovers from the old naming, every read-only page and
          diagnostic, login/session security.
  ui      the same browser checks as the local tier, against the real Pi.
  reboot  OPT-IN: clicks Restart in the UI and checks the Pi comes back
          healthy. Never part of the default run.

No tier = local + smoke + ui. "all" = those plus reboot. The Pi tiers read
their settings (pi_host, pi_user, ssh_private_key_path, mode, psk) from the
tfvars file WGA_CONFIG points at - the same one ./wga uses. The password is
only ever held in memory: never printed, logged or written anywhere.
"""
import html
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TIERS = ("local", "smoke", "ui", "reboot")

# Scripts the deploy uploads and the web service (mnh-web) has to be able to
# read but never change. Kept in step with main.tf's local.scripts.
DEPLOYED_SCRIPTS = [
    "setup_host.sh", "setup_wireguard.sh", "setup_forwarding_and_nat.sh",
    "setup_ap.sh", "uplink_wifi.sh", "view_currently_associated_clients.sh",
    "view_wireguard_status.sh", "diagnostics.sh", "diagnostics_sudo.sh",
    "diagnostics_lib.sh", "setup_webapp.sh", "shutdown_pi.sh",
]


# --------------------------------------------------------------------------
# Configuration (the Pi tiers)
# --------------------------------------------------------------------------
def _tfvar(text: str, name: str) -> str | None:
    match = re.search(rf'^\s*{name}\s*=\s*"((?:[^"\\]|\\.)*)"', text, re.M)
    return re.sub(r"\\(.)", r"\1", match.group(1)) if match else None


@dataclass
class PiConfig:
    host: str
    user: str
    key: str | None
    mode: str
    psk: str = ""  # never printed: excluded from repr

    def __repr__(self) -> str:
        return f"PiConfig({self.user}@{self.host}, mode={self.mode})"

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    def ssh(self, command: str, timeout: float = 30) -> subprocess.CompletedProcess:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if self.key:
            args += ["-i", self.key]
        return subprocess.run(
            args + [f"{self.user}@{self.host}", command],
            capture_output=True, text=True, timeout=timeout,
        )

    def out(self, command: str) -> str:
        return self.ssh(command).stdout


@pytest.fixture(scope="session")
def pi() -> PiConfig:
    path = os.environ.get("WGA_CONFIG")
    if not path or not Path(path).is_file():
        pytest.fail("WGA_CONFIG must point at your wireguard-ap.tfvars for the Pi tiers "
                    "(or run just './wga test local')")
    text = Path(path).read_text()
    cfg = PiConfig(
        host=_tfvar(text, "pi_host") or "",
        user=_tfvar(text, "pi_user") or "",
        key=_tfvar(text, "ssh_private_key_path") or None,
        mode=_tfvar(text, "mode") or "dual",
        psk=_tfvar(text, "psk") or "",
    )
    if not (cfg.host and cfg.user and cfg.psk):
        pytest.fail("pi_host, pi_user and psk must all be set in the tfvars")
    if cfg.ssh("true").returncode != 0:
        pytest.fail(f"cannot SSH to {cfg.user}@{cfg.host}")
    return cfg


# --------------------------------------------------------------------------
# A logged-in web client, for both the local server and the Pi
# --------------------------------------------------------------------------
@dataclass
class Result:
    status: int
    out: str = ""


class Web:
    def __init__(self, base_url: str, password: str):
        self.base_url = base_url
        self._password = password
        self.client = httpx.Client(base_url=base_url, verify=False, timeout=30,
                                   follow_redirects=False)

    def login(self) -> httpx.Response:
        return self.client.post("/login", data={"password": self._password})

    def get(self, path: str) -> httpx.Response:
        return self.client.get(path)

    def start(self, command: str, *params: str) -> tuple[httpx.Response, str | None]:
        # A dict with a list value is how httpx sends a repeated form field.
        r = self.client.post("/run/diagnostics", data={"command": command, "param": list(params)})
        match = re.search(r"/run/stream/([0-9a-f]+)", r.text)
        return r, match.group(1) if match else None

    def run(self, command: str, *params: str, timeout: float = 45) -> Result:
        """Runs a diagnostic the way the page does: POST, then read the SSE
        stream to its end. Returns the app's status (400 = rejected) and the
        command's output as plain text."""
        r, job = self.start(command, *params)
        if job is None:
            return Result(r.status_code, r.text)
        lines = []
        with self.client.stream("GET", f"/run/stream/{job}", timeout=timeout) as s:
            for line in s.iter_lines():
                if line.startswith("data:"):
                    lines.append(html.unescape(line[5:].lstrip(" ")).replace("<br>", "\n"))
        return Result(200, "".join(lines))


@pytest.fixture(scope="session")
def pi_web(pi) -> Web:
    web = Web(pi.base_url, pi.psk)
    assert web.login().status_code == 303, "login was refused"
    return web


# --------------------------------------------------------------------------
# A throwaway local copy of the web app (never touches any other server)
# --------------------------------------------------------------------------
LOCAL_PSK = "integration-test-password"


@dataclass
class Target:
    name: str
    base_url: str
    password: str
    ping_running: Callable[[], bool]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def local_server():
    if not shutil.which("uv") or not shutil.which("openssl"):
        pytest.fail("the local tier needs uv and openssl on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="wga-it-"))
    port = _free_port()
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", tmp / "k.pem",
         "-out", tmp / "c.pem", "-days", "1", "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    env = {
        **os.environ,
        "SCRIPTS_DIR": str(SCRIPTS), "STATE_DIR": str(tmp), "SSID": "integration",
        "PSK": LOCAL_PSK, "WEBAPP_HOST": "127.0.0.1", "WEBAPP_PORT": str(port),
        "WEBAPP_CERT": str(tmp / "c.pem"), "WEBAPP_KEY": str(tmp / "k.pem"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    log = open(tmp / "server.log", "w")
    # Own process group, so teardown stops exactly what this started.
    proc = subprocess.Popen(["uv", "run", "app.py"], cwd=REPO / "webapp", env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"https://127.0.0.1:{port}"
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if httpx.get(base + "/healthz", verify=False, timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail(f"local server did not start; see {tmp / 'server.log'}")
        yield Target("local", base, LOCAL_PSK,
                     lambda: subprocess.run(["pgrep", "-f", "ping -W 3 12[7].0.0.1"],
                                            capture_output=True).returncode == 0)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# LOCAL: the diagnostics scripts, no Pi and no server
# ==========================================================================
def script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPTS / "diagnostics.sh"), *args],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.local
class TestDiagnosticsScript:
    def test_show_commands_protocol(self):
        r = script("--show-commands")
        assert r.returncode == 0
        commands = {}
        for line in r.stdout.splitlines():
            sig, _, desc = line.partition("|")
            words = sig.split()
            commands[words[0]] = words[1:]
            assert desc.strip(), f"no description: {line!r}"
        expected = {"ping", "traceroute", "dig", "ip-addr-list", "ip-route-list",
                    "ip-route-get", "ap-clients", "service-status", "logs",
                    "wg-status"}  # wg-status comes from diagnostics_sudo.sh
        assert expected <= set(commands), expected - set(commands)
        assert commands["dig"][0] == "name" and commands["dig"][1] == "server?"
        assert commands["dig"][2].startswith("type?=A,")
        assert commands["ping"] == ["host"] and commands["ip-addr-list"] == []

    def test_root_only_commands_are_not_defined_in_diagnostics_sh(self):
        assert "wg-status" not in (SCRIPTS / "diagnostics.sh").read_text().replace(
            "wg-status, ap-clients", "")
        r = subprocess.run([str(SCRIPTS / "diagnostics_sudo.sh"), "--show-commands"],
                           capture_output=True, text=True)
        assert [l.split()[0] for l in r.stdout.splitlines()] == ["wg-status"]

    @pytest.mark.parametrize("bad", [
        "127.0.0.1;touch {m}", "$(touch {m})", "`touch {m}`", "127.0.0.1\ntouch {m}",
        "127.0.0.1 && touch {m}", "127.0.0.1|touch {m}", "-f", "--help", "-c1",
        "127.0.0.1 -c1", "", " ",
    ])
    @pytest.mark.parametrize("cmd", ["ping", "traceroute", "ip-route-get"])
    def test_host_params_reject_injection(self, cmd, bad, tmp_path):
        marker = tmp_path / "pwned"
        value = bad.format(m=marker)
        r = script("--run-command", cmd, value)
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert not marker.exists()

    @pytest.mark.parametrize("name,server,type_", [
        ("example.com", "-x", ""), ("example.com", "1.1.1.1 +tcp", ""),
        ("-x", "", ""), ("example.com;id", "", ""), ("example.com", "$(id)", ""),
        ("example.com", "", "BOGUS"), ("example.com", "", "A,MX"),
        ("example.com", "", "A;id"),
    ])
    def test_dig_params_are_validated(self, name, server, type_):
        r = script("--run-command", "dig", name, server, type_)
        assert r.returncode == 2, (r.returncode, r.stderr)

    def test_dig_needs_a_name_but_not_the_optionals(self):
        assert "name is required" in script("--run-command", "dig", "", "", "").stderr
        r = script("--run-command", "dig", "example.com", "", "")
        assert r.returncode in (0, 127)  # 127 = dig not installed here

    @pytest.mark.parametrize("args", [
        ["nope", ], ["ping$(touch x)", "127.0.0.1"], ["x[$(id)]"], ["PARAMS[a]"], [""],
    ])
    def test_unknown_command_names_are_refused(self, args):
        assert script("--run-command", *args).returncode == 2

    @pytest.mark.parametrize("unit,lines", [
        ("ssh", "50"), ("dnsmasq", "7"), ("dnsmasq,wg-quick@wg0", "50"),
        ("dnsmasq", "50,100"), ("", "50"), ("dnsmasq", ""),
        ("mnh-ap-nat,mnh-ap-webapp", "50"), ("*", "50"),
    ])
    def test_choice_params_only_accept_listed_values(self, unit, lines):
        assert script("--run-command", "logs", unit, lines).returncode == 2

    @pytest.mark.parametrize("args", [
        ("ping",), ("ping", "127.0.0.1", "extra"), ("ip-addr-list", "x"),
        ("logs", "dnsmasq"),
    ])
    def test_wrong_argument_count_is_refused(self, args):
        assert script("--run-command", *args).returncode == 2

    def test_sudo_script_only_knows_its_own_commands(self):
        for args in (["ping", "127.0.0.1"], ["nope"], ["wg-status$(id)"]):
            r = subprocess.run([str(SCRIPTS / "diagnostics_sudo.sh"), "--run-command", *args],
                               capture_output=True, text=True)
            assert r.returncode == 2 and "unknown command" in r.stderr

    @pytest.mark.parametrize("name", sorted(p.name for p in SCRIPTS.glob("*.sh")))
    def test_script_syntax(self, name):
        assert subprocess.run(["bash", "-n", str(SCRIPTS / name)]).returncode == 0

    def test_generated_sudoers_for_the_web_user_is_valid(self, tmp_path):
        visudo = shutil.which("visudo")
        if not visudo:
            pytest.skip("visudo not installed")
        src = (SCRIPTS / "setup_webapp.sh").read_text()
        body = re.search(r'cat >"\$SUDOERS_TMP" <<EOF\n(.*?)\nEOF', src, re.S).group(1)
        rendered = body.replace("${WEB_USER}", "mnh-web").replace("${SCRIPTS_DIR}", "/home/x")
        f = tmp_path / "sudoers"
        f.write_text(rendered + "\n")
        r = subprocess.run([visudo, "-cf", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        # Fixed-argument rules must stay fixed: nothing broader than intended.
        assert 'setup_ap.sh dual' in rendered and 'setup_ap.sh uplink 5' in rendered
        assert 'setup_ap.sh *' not in rendered and 'ALL=(ALL)' not in rendered
        assert rendered.index("SETENV:") > rendered.index("uplink_wifi.sh")


# ==========================================================================
# SMOKE: the deployed Pi
# ==========================================================================
def expected_units(pi: PiConfig) -> list[str]:
    hostapd = ["mnh-hostapd@wlan0", "mnh-hostapd@wlan1"] if pi.mode == "dual" \
        else ["mnh-hostapd@wlan1"]
    return ["mnh-ap-webapp", "mnh-ap-nat", "mnh-ap-local-routing", "mnh-ap-wg-watchdog.timer",
            "wg-quick@wg0", "dnsmasq", "NetworkManager", *hostapd]


def pi_health_problems(pi: PiConfig) -> list[str]:
    """The checks worth repeating after a reboot. Empty list = healthy."""
    problems = []
    for unit in expected_units(pi):
        state = pi.out(f"systemctl is-active {unit}").strip()
        if state != "active":
            problems.append(f"{unit} is {state or 'unknown'}")
    if pi.out("systemctl --failed --no-legend").strip():
        problems.append("there are failed units")
    if "fwmark" not in pi.out("ip rule"):
        problems.append("routing-protection rules (fwmark) are missing")
    if ":443 " not in pi.out("ss -ltn"):
        problems.append("nothing is listening on 443")
    if not pi.out("ps -o user= -C python").split() or "mnh-web" not in pi.out("ps -o user= -C python"):
        problems.append("the web app is not running as mnh-web")
    return problems


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


# ==========================================================================
# UI: the browser, against the local server (tier local) and the Pi (tier ui)
# ==========================================================================
@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - the message is the useful part
            pytest.fail("could not launch Chromium - run: uv run --with playwright "
                        f"playwright install chromium ({str(e).splitlines()[0]})")
        yield b
        b.close()


@pytest.fixture(params=[pytest.param("local", marks=pytest.mark.local),
                        pytest.param("pi", marks=pytest.mark.ui)])
def target(request) -> Target:
    if request.param == "local":
        return request.getfixturevalue("local_server")
    pi = request.getfixturevalue("pi")
    return Target("pi", pi.base_url, pi.psk,
                  lambda: pi.ssh("pgrep -f 'ping -W 3 12[7].0.0.1' >/dev/null").returncode == 0)


@pytest.fixture
def page(browser, target):
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1100, "height": 900})
    try:
        ok = ctx.request.post(target.base_url + "/login", form={"password": target.password},
                              max_redirects=0).status == 303
    except Exception:  # noqa: BLE001 - never surface the request (holds the password)
        ok = False
    assert ok, "UI login failed"
    pg = ctx.new_page()
    pg.goto(target.base_url + "/diagnostics")
    yield pg
    ctx.close()


def visible_cards(page):
    return page.locator("article[data-command]:visible")


@pytest.mark.usefixtures("target")
class TestDiagnosticsUI:
    def test_titles_and_nav_order(self, page, target):
        assert page.title() == "mnh-ap - Diagnostics"
        nav = page.locator("nav a").all_inner_texts()
        assert nav.index("Diagnostics") < nav.index("Manage")
        page.goto(target.base_url + "/")
        assert page.title() == "mnh-ap"

    def test_starts_with_a_placeholder_and_no_card(self, page):
        sel = page.locator("#diagnostic-select")
        assert "Choose a diagnostic" in sel.evaluate("s => s.options[s.selectedIndex].text")
        assert sel.evaluate("s => s.options[0].hidden") is True  # not offered in the list
        assert visible_cards(page).count() == 0

    def test_picker_sits_next_to_its_label_and_is_only_as_wide_as_needed(self, page):
        sel = page.locator("#diagnostic-select").bounding_box()
        label = page.locator("label[for=diagnostic-select]").bounding_box()
        assert abs(sel["y"] - label["y"]) < 20 and sel["width"] < 400

    def test_choosing_shows_only_that_card_with_description_and_no_title(self, page):
        page.locator("#diagnostic-select").select_option("ping")
        cards = visible_cards(page)
        assert cards.count() == 1 and cards.first.get_attribute("data-command") == "ping"
        assert "Ping a host" in cards.first.inner_text()
        assert cards.first.locator("header").count() == 0

    def test_run_button_is_at_the_right_of_the_card(self, page):
        page.locator("#diagnostic-select").select_option("ping")
        card = visible_cards(page).first.bounding_box()
        btn = visible_cards(page).first.locator("button").bounding_box()
        assert (card["x"] + card["width"]) - (btn["x"] + btn["width"]) < 60 and btn["width"] < 200

    def test_a_required_field_must_be_filled_before_anything_runs(self, page):
        page.locator("#diagnostic-select").select_option("ping")
        visible_cards(page).first.locator("button").click()
        assert not page.locator("#output-dialog").evaluate("d => d.open")

    def test_optional_and_choice_fields_render_correctly(self, page):
        sel = page.locator("#diagnostic-select")
        sel.select_option("dig")
        dig = visible_cards(page).first
        assert dig.locator("input[required]").count() == 1
        assert dig.locator("input[type=text]:not([required])").count() == 1
        assert dig.locator("select option").first.get_attribute("value") == ""
        assert dig.inner_text().count("(optional)") == 2
        sel.select_option("logs")
        assert visible_cards(page).first.locator("select[required]").count() == 2

    @pytest.mark.parametrize("close", ["button", "escape"])
    def test_ping_runs_until_the_dialog_is_closed_then_stops(self, page, target, close):
        from playwright.sync_api import expect

        page.locator("#diagnostic-select").select_option("ping")
        card = visible_cards(page).first
        card.locator("input[type=text]").fill("127.0.0.1")
        card.locator("button").click()
        expect(page.locator("#output-dialog")).to_be_visible()
        assert page.locator("#output-heading").inner_text() == "ping output"
        expect(page.locator("#job-output")).to_contain_text("bytes from", timeout=15000)
        seen = page.locator("#job-output").inner_text().count("bytes from")
        time.sleep(3)
        assert page.locator("#job-output").inner_text().count("bytes from") > seen
        assert target.ping_running()
        if close == "button":
            page.click("#output-dialog footer a")
        else:
            page.keyboard.press("Escape")
        deadline = time.time() + 10
        while time.time() < deadline and target.ping_running():
            time.sleep(1)
        assert not target.ping_running()
        assert page.locator("#job-output").inner_text().strip() == ""
        # ...and it can be run again straight away.
        card.locator("button").click()
        expect(page.locator("#job-output")).to_contain_text("bytes from", timeout=15000)
        page.keyboard.press("Escape")

    def test_long_output_follows_at_the_bottom(self, page):
        from playwright.sync_api import expect

        page.locator("#diagnostic-select").select_option("ip-addr-list")
        visible_cards(page).first.locator("button").click()
        expect(page.locator("#job-output")).to_contain_text("[exit code", timeout=15000)
        d = page.locator("#job-output").evaluate(
            "e => ({top: e.scrollTop, h: e.scrollHeight, c: e.clientHeight})")
        assert d["h"] - d["top"] - d["c"] <= 4, d  # at the bottom (whether or not it overflowed)

    def test_reload_keeps_the_cards_in_step_with_the_picker(self, page):
        page.locator("#diagnostic-select").select_option("traceroute")
        page.reload()
        chosen = page.locator("#diagnostic-select").evaluate("s => s.value")
        assert visible_cards(page).count() == (1 if chosen else 0)


# ==========================================================================
# REBOOT (opt-in): the Restart button, end to end. Keep this LAST.
# ==========================================================================
@pytest.mark.reboot
def test_restart_button_reboots_the_pi_and_everything_comes_back(pi, browser):
    before = pi.out("cat /proc/sys/kernel/random/boot_id").strip()
    assert before
    ctx = browser.new_context(ignore_https_errors=True)
    assert ctx.request.post(pi.base_url + "/login", form={"password": pi.psk},
                            max_redirects=0).status == 303
    pg = ctx.new_page()
    pg.goto(pi.base_url + "/manage")
    pg.click("text=Restart the Pi")
    pg.wait_for_selector("#restart-dialog[open]")
    pg.click('#restart-dialog a[hx-post="/restart"]')
    pg.wait_for_url("**/shutting-down?action=restart", timeout=15000)
    pg.wait_for_url("**/login", timeout=300000)  # the page polls until the Pi is back
    ctx.close()

    deadline = time.time() + 180
    after = ""
    while time.time() < deadline and (not after or after == before):
        try:
            r = pi.ssh("cat /proc/sys/kernel/random/boot_id", timeout=15)
            after = r.stdout.strip() if r.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            after = ""
        time.sleep(3)
    assert after and after != before, "the Pi never reported a new boot id"

    problems = ["not checked"]
    deadline = time.time() + 180  # boot-time services take a while to settle
    while time.time() < deadline:
        problems = pi_health_problems(pi)
        if not problems:
            break
        time.sleep(5)
    assert not problems, problems
    assert httpx.get(pi.base_url + "/healthz", verify=False, timeout=10).status_code == 200


# --------------------------------------------------------------------------
def main() -> int:
    args = sys.argv[1:]
    chosen = [a for a in args if a in (*TIERS, "all")]
    rest = [a for a in args if a not in (*TIERS, "all")]
    if "all" in chosen:
        expr = " or ".join(TIERS)
    elif chosen:
        expr = " or ".join(chosen)
    else:
        expr = "local or smoke or ui"
    return pytest.main([
        __file__, "-m", expr, "-q", "-ra", "--tb=short", "-p", "no:cacheprovider",
        "-W", "ignore::pytest.PytestUnknownMarkWarning",
        *rest,
    ])


if __name__ == "__main__":
    sys.exit(main())
