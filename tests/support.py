"""Shared by the integration tests: constants, the Pi config, the web client and
the health checks. Plain code only - the fixtures are in conftest.py.
"""
import html
import re
import socket
import subprocess
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
