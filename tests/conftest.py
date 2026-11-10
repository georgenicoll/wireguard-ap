"""Fixtures for the integration tests (see integration.py for the tiers)."""
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from support import tfvar, free_port, REPO, SCRIPTS, PiConfig, Web, LOCAL_PSK, Target, FakeCollector


def pytest_configure(config):
    for tier, what in (("local", "no Pi needed"), ("smoke", "the deployed Pi"),
                       ("ui", "the browser, against the Pi"),
                       ("reboot", "opt-in: restarts the Pi")):
        config.addinivalue_line("markers", f"{tier}: {what}")


@pytest.fixture(scope="session")
def pi() -> PiConfig:
    path = os.environ.get("WGA_CONFIG")
    if not path or not Path(path).is_file():
        pytest.fail("WGA_CONFIG must point at your wireguard-ap.tfvars for the Pi tiers "
                    "(or run just './wga test local')")
    text = Path(path).read_text()
    cfg = PiConfig(
        host=tfvar(text, "pi_host") or "",
        user=tfvar(text, "pi_user") or "",
        key=tfvar(text, "ssh_private_key_path") or None,
        mode=tfvar(text, "mode") or "dual",
        psk=tfvar(text, "psk") or "",
    )
    if not (cfg.host and cfg.user and cfg.psk):
        pytest.fail("pi_host, pi_user and psk must all be set in the tfvars")
    if cfg.ssh("true").returncode != 0:
        pytest.fail(f"cannot SSH to {cfg.user}@{cfg.host}")
    return cfg


@pytest.fixture(scope="session")
def pi_web(pi) -> Web:
    web = Web(pi.base_url, pi.psk)
    assert web.login().status_code == 303, "login was refused"
    return web


@pytest.fixture(scope="session")
def fake_collector():
    """A stand-in for the metrics collector, which the local web app talks to."""
    directory = Path(tempfile.mkdtemp(prefix="wga-fc-"))
    collector = FakeCollector(directory / "metrics.sock")
    collector.start()
    yield collector
    collector.stop()
    shutil.rmtree(directory, ignore_errors=True)



@pytest.fixture(scope="session")
def smweb_binary() -> Path:
    """smweb, the web server for the Metrics page, for the local web app to
    talk to. In order of preference: SMWEB_BIN; the release pinned in
    simple-metrics.pin - the very binary that gets deployed - where this
    machine can run the Pi's (static, aarch64) build; or one built from a
    simple-metrics checkout beside this repository."""
    explicit = os.environ.get("SMWEB_BIN")
    if explicit:
        if not os.access(explicit, os.X_OK):
            pytest.fail(f"SMWEB_BIN={explicit} is not an executable file")
        return Path(explicit)
    if platform.machine() in ("aarch64", "arm64") and (REPO / "simple-metrics.pin").exists():
        fetched = subprocess.run([str(REPO / "tools" / "fetch_simple_metrics.sh"), "--web"],
                                 capture_output=True, text=True, timeout=300)
        if fetched.returncode == 0 and fetched.stdout.strip():
            return Path(fetched.stdout.strip())
    sibling = REPO.parent / "simple-metrics"
    if (sibling / "Cargo.toml").exists() and shutil.which("cargo"):
        built = subprocess.run(["cargo", "build", "--quiet", "--bin", "smweb"], cwd=sibling,
                               capture_output=True, text=True, timeout=900)
        if built.returncode != 0:
            pytest.fail(f"building smweb in {sibling} failed:\n{built.stderr[-2000:]}")
        return sibling / "target" / "debug" / "smweb"
    pytest.fail("the local tier needs smweb (the web server for the Metrics page): set SMWEB_BIN "
                "to one, or check out github.com/georgenicoll/simple-metrics beside this "
                "repository (with cargo installed), or run on an aarch64 machine, which can "
                "use the pinned release")


class SmwebProcess:
    """The real smweb, reading the fake collector and serving a Unix socket,
    which can be stopped and started to see how the web app copes."""

    def __init__(self, binary: Path, collector_socket: Path, socket: Path):
        self.binary, self.collector_socket, self.socket = binary, collector_socket, socket
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self.socket.unlink(missing_ok=True)
        # Mounted at /metrics, as the deployed service is (setup_metrics.sh).
        self.proc = subprocess.Popen(
            [str(self.binary), "--socket", str(self.collector_socket), "--unix", str(self.socket),
             "--unix-mode", "600", "--base-path", "/metrics", "--title", "mnh-ap"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while not self.socket.exists():
            if self.proc.poll() is not None or time.time() > deadline:
                pytest.fail("smweb did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        self.proc = None
        self.socket.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def smweb(fake_collector, smweb_binary):
    directory = Path(tempfile.mkdtemp(prefix="wga-sw-"))
    process = SmwebProcess(smweb_binary, fake_collector.path, directory / "smweb.sock")
    process.start()
    yield process
    process.stop()
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def smweb_service(smweb):
    """smweb, running again before and after each test (a test may stop it)."""
    smweb.start()
    yield smweb
    smweb.start()

@pytest.fixture
def collector(fake_collector):
    """The fake collector, put back to normal before and after each test."""
    fake_collector.reset()
    yield fake_collector
    fake_collector.reset()


@pytest.fixture(scope="session")
def local_server(fake_collector, smweb):
    if not shutil.which("uv") or not shutil.which("openssl"):
        pytest.fail("the local tier needs uv and openssl on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="wga-it-"))
    port = free_port()
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
        "METRICS_WEB_SOCKET": str(smweb.socket),
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
                                            capture_output=True).returncode == 0,
                     state_dir=tmp)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        shutil.rmtree(tmp, ignore_errors=True)


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
