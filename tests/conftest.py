"""Fixtures for the integration tests (see integration.py for the tiers)."""
import os
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


@pytest.fixture
def collector(fake_collector):
    """The fake collector, put back to normal before and after each test."""
    fake_collector.reset()
    yield fake_collector
    fake_collector.reset()


@pytest.fixture(scope="session")
def local_server(fake_collector):
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
        "METRICS_SOCKET": str(fake_collector.path),
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
