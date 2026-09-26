"""Shared by the integration tests: constants, the Pi config, the web client and
the health checks. Plain code only - the fixtures are in conftest.py.
"""
import html
import json
import re
import socket
import socketserver
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

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
    # Uploaded by main.tf's metrics_deploy rather than listed in local.scripts.
    "setup_metrics.sh",
]


# --------------------------------------------------------------------------
# Configuration of the Pi tiers
# --------------------------------------------------------------------------
def tfvar(text: str, name: str) -> str | None:
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
    state_dir: Path | None = None  # the local server's, where its session secret is


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# Checks of the deployed Pi shared by the smoke and reboot tiers
# --------------------------------------------------------------------------
def expected_units(pi: PiConfig) -> list[str]:
    hostapd = ["mnh-hostapd@wlan0", "mnh-hostapd@wlan1"] if pi.mode == "dual" \
        else ["mnh-hostapd@wlan1"]
    return ["mnh-ap-webapp", "mnh-ap-metrics", "mnh-ap-nat", "mnh-ap-local-routing",
            "mnh-ap-wg-watchdog.timer",
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


# --------------------------------------------------------------------------
# A stand-in for the simple-metrics collector, for testing the web app
# --------------------------------------------------------------------------
class FakeCollector:
    """Speaks the collector's JSON-lines protocol on a Unix socket, giving
    made-up but plausible data, and remembers every request it was sent so a
    test can check exactly what the web app asked for.

    `mode` is "ok", "error" (answers every request with a failure) or "down"
    (hangs up without answering, as a crashed collector would)."""

    METRICS = [
        {"name": "cpu_percent", "label": "CPU", "unit": "%"},
        {"name": "load1", "label": "Load (1 min)", "unit": ""},
        {"name": "mem_used_bytes", "label": "Memory used", "unit": "bytes"},
        {"name": "cpu_temp_celsius", "label": "CPU temperature", "unit": "\u00b0C"},
        {"name": "net_eth0_rx_bytes_per_sec", "label": "eth0 received", "unit": "bytes/s"},
    ]

    def __init__(self, path: Path):
        self.path = path
        self.mode = "ok"
        self.requests: list[dict] = []
        collector = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for line in self.rfile:
                    request = json.loads(line)
                    collector.requests.append(request)
                    if collector.mode == "down":
                        return
                    reply = collector.reply(request)
                    self.wfile.write(json.dumps(reply).encode() + b"\n")

        self.server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def reset(self) -> None:
        self.mode = "ok"
        self.requests.clear()

    def reply(self, request: dict) -> dict:
        if self.mode == "error":
            return {"ok": False, "v": 1, "error": "boom"}
        op = request.get("op")
        if op == "metrics":
            return {"ok": True, "v": 1, "metrics": self.METRICS}
        if op == "read":
            return self.read(request)
        return {"ok": False, "v": 1, "error": f"unknown op {op!r}"}

    def read(self, request: dict) -> dict:
        import math
        import time

        names = request["metrics"]
        now = int(time.time() * 1000)
        start = request.get("from", now - 3_600_000)
        step = max(5000, -(-(now - start) // request["max_points"]))
        step = -(-step // 5000) * 5000
        stamps = list(range(start - start % step, now + 1, step))
        avg = {n: [] for n in names}
        for i, t in enumerate(stamps):
            for n in names:
                # One gap, in the middle, so the chart has something to break on.
                avg[n].append(None if i == len(stamps) // 2 else 50 + 30 * math.sin(t / 600_000))
        scale = lambda cols, f: {n: [None if v is None else v * f for v in c] for n, c in cols.items()}
        return {"ok": True, "v": 1, "step_ms": step, "timestamps": stamps, "series": avg,
                "min": scale(avg, 0.8), "max": scale(avg, 1.2)}
