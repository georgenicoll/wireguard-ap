# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "jinja2",
#   "itsdangerous",
#   "python-multipart",
# ]
# ///
import asyncio
import html
import logging
import os
import re
import secrets
import socket
import subprocess
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
# Where the setup scripts live - one level up, alongside webapp/ itself
# (see main.tf's remote_dir). Only used here for view_wireguard_status.sh,
# to get at "wg show"'s root-only handshake info via its existing
# self-elevation (see the README's sudoers section) rather than trying to
# sudo straight from the app, which has no sudoers coverage to do that.
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", BASE_DIR.parent))

# Interfaces the AP setup can create (see setup_ap.sh/setup_host.sh) -
# eth0 the wired uplink, wlan0/wlan1 the two radios (AP and/or Wi-Fi
# uplink client depending on mode). IF_24/IF_5/SSID reach the app the same
# way PSK does (EnvironmentFile= in the systemd unit, sourced from
# wireguard-ap.env) - IF_24/IF_5 default to setup_ap.sh's own examples for
# local dev, where that env file doesn't exist.
INTERFACES = ["eth0", "wlan0", "wlan1"]
IF_24 = os.environ.get("IF_24", "wlan0")
IF_5 = os.environ.get("IF_5", "wlan1")
BR = os.environ.get("BR", "br-ap")
SSID = os.environ.get("SSID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)


def _run(cmd: list[str], timeout: float = 3) -> str:
    # Command may not even exist here (e.g. nmcli/iw, both Pi-only - see
    # dev_webapp.sh) - caught the same as a failed/timed-out run, since
    # either way there's just no data to show.
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _run_script(name: str, timeout: float = 10) -> str:
    # Both scripts self-elevate internally (sudo, NOPASSWD - see the
    # README's sudoers section) rather than being invoked with sudo here.
    # Not found at all locally (SCRIPTS_DIR matches the Pi's flat layout,
    # not this repo's scripts/ subdirectory) - caught and surfaced inline
    # (the AP/WireGuard Details pages show this text directly) rather
    # than a hard 500.
    script = SCRIPTS_DIR / name
    LOGGER.info("Attempting to run script: %s", str(script))
    try:
        result = subprocess.run(
            [str(script)], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        LOGGER.info("Failed to run: %s - %s", str(script), str(e))
        return f"Failed to run {name}: {e}"
    LOGGER.info("Finished running: %s", str(script))
    output = result.stdout
    if result.returncode != 0:
        output += f"\n(exit code {result.returncode})\n{result.stderr}"
    return output


# In-memory only, by design: this is a single-user local admin tool (one
# process, no persistence needed), not a multi-tenant job queue. A job
# started but never streamed (e.g. the tab was closed before the
# EventSource opened) leaks its dict entry until the process exits, but
# never runs longer than the underlying script does.
_jobs: dict[str, asyncio.subprocess.Process] = {}


async def _start_job(name: str, args: list[str]) -> str:
    # setup_ap.sh and uplink_wifi.sh both self-elevate internally (sudo,
    # NOPASSWD - see the README's sudoers section), same as _run_script.
    process = await asyncio.create_subprocess_exec(
        str(SCRIPTS_DIR / name),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    job_id = uuid.uuid4().hex
    _jobs[job_id] = process
    return job_id


def _sse_output_fragment(job_id: str, output_id: str) -> str:
    # htmx's SSE extension swaps each event's data in via innerHTML (see
    # _stream_job, which HTML-escapes it accordingly) using this element's
    # own hx-swap - "beforeend" here, so each message is appended rather
    # than replacing the previous ones. Keeps the same id as the original
    # placeholder (an outerHTML swap replaces the element entirely), so the
    # form's hx-target selector still finds it on a second run. job_id and
    # output_id are both server-generated (uuid4 / a fixed literal), never
    # user input, so no escaping is needed building this fragment.
    return (
        f'<pre id="{output_id}" class="output" hx-ext="sse" '
        f'sse-connect="/run/stream/{job_id}" sse-swap="message" '
        f'sse-close="done" hx-swap="beforeend"></pre>'
    )


async def _stream_job(job_id: str):
    process = _jobs.get(job_id)
    if process is None or process.stdout is None:
        yield "event: done\ndata: (no such job - already finished, or never started)\n\n"
        return
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        text = html.escape(line.decode(errors="replace").rstrip("\n"))
        yield f"data: {text}<br>\n\n"
    returncode = await process.wait()
    yield f"data: <br>[exit code {returncode}]<br>\n\n"
    yield "event: done\ndata: \n\n"
    _jobs.pop(job_id, None)


def _interface_info(name: str) -> dict:
    # Same tool view_currently_associated_clients.sh uses (nmcli device
    # status) - queried per-device here instead of as one table, since we
    # want specific fields (state, IP) rather than a printed block.
    state_out = _run(["nmcli", "-g", "GENERAL.STATE", "device", "show", name]).strip()
    if not state_out:
        return {"name": name, "status": "not found", "ip": None}
    match = re.search(r"\((.*?)\)", state_out)
    status = (match.group(1) if match else state_out).upper()
    ip_out = _run(["nmcli", "-g", "IP4.ADDRESS", "device", "show", name]).strip()
    ip = ip_out.split("|")[0].split("/")[0] if ip_out else None
    return {"name": name, "status": status, "ip": ip}


def _ap_interfaces() -> list[str]:
    # Same commands view_currently_associated_clients.sh uses to find the
    # AP-mode interfaces (iw dev, looking for "type AP" entries).
    aps = []
    current = None
    for line in _run(["iw", "dev"]).splitlines():
        line = line.strip()
        if line.startswith("Interface"):
            current = line.split()[1]
        elif line.startswith("type AP") and current:
            aps.append(current)
    return aps


def _ap_band(iface: str) -> str:
    info = _run(["iw", "dev", iface, "info"])
    match = re.search(r"channel \d+ \((\d+) MHz\)", info)
    if not match:
        return "unknown band"
    return "2.4G" if int(match.group(1)) < 3000 else "5G"


def _mode() -> str:
    # dual: both radios (IF_24/IF_5) broadcast the AP. uplink: only IF_5
    # does (at whichever band was chosen - see setup_ap.sh), IF_24 is
    # freed up as a Wi-Fi client instead.
    aps = set(_ap_interfaces())
    if IF_24 in aps and IF_5 in aps:
        return "dual"
    if IF_5 in aps:
        return f"uplink on {_ap_band(IF_5)}"
    return "unknown"


def _connected_client_count() -> int:
    # Same commands view_currently_associated_clients.sh uses to list
    # clients (iw dev to find AP-mode interfaces, then station dump on
    # each) - just counted here rather than printed in full.
    return sum(
        _run(["iw", "dev", ap, "station", "dump"]).count("Station ")
        for ap in _ap_interfaces()
    )


def _wireguard_ip() -> str | None:
    # No root needed for this (unlike the handshake info below) - "ip
    # addr" just reads interface state, same as "ip link" already does
    # for the interface table.
    out = _run(["ip", "-4", "-o", "addr", "show", "wg0"])
    match = re.search(r"inet (\S+)/", out)
    return match.group(1) if match else None


def _wireguard_peer_info() -> dict:
    # "wg show" needs root, unlike everything else on this page - reuses
    # view_wireguard_status.sh's own self-elevation (see _run_script)
    # rather than the app sudo-ing directly, which isn't covered by the
    # project's sudoers file (scoped to exact script paths - see the
    # README). Parses the same human-readable lines "wg show" already
    # prints (one script call covers both fields) rather than re-deriving
    # them from "latest-handshakes"/"dump".
    out = _run_script("view_wireguard_status.sh")
    handshake = re.search(r"latest handshake:\s*(.+)", out)
    endpoint = re.search(r"endpoint:\s*(\S+)", out)
    return {
        "handshake": handshake.group(1).strip() if handshake else "unknown",
        "peer": endpoint.group(1) if endpoint else "not found",
    }


def host_info() -> dict:
    ap_ifaces = set(_ap_interfaces())
    ips = [
        info
        for name in INTERFACES
        if name not in ap_ifaces
        for info in [_interface_info(name)]
        if info["ip"]
    ]
    return {
        "name": socket.gethostname(),
        "mode": _mode(),
        "uptime": _run(["uptime", "-p"]).strip() or "unknown",
        "ips": ips,
    }


def ap_info() -> dict:
    return {
        "ssid": SSID,
        "ip": _interface_info(BR)["ip"],
        "client_count": _connected_client_count(),
    }


def wireguard_info() -> dict:
    return {
        "ip": _wireguard_ip(),
        **_wireguard_peer_info(),
    }


# The AP's own Wi-Fi password, doubling as this site's login password -
# one shared secret rather than a separate site password to remember.
PSK = os.environ.get("PSK", "")

# Persisted so a service restart (e.g. a redeploy) doesn't invalidate every
# existing login session - generated once, on first run.
SECRET_FILE = BASE_DIR / ".session_secret"
if SECRET_FILE.exists():
    SESSION_SECRET = SECRET_FILE.read_text().strip()
else:
    SESSION_SECRET = secrets.token_hex(32)
    SECRET_FILE.write_text(SESSION_SECRET)
    SECRET_FILE.chmod(0o600)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=True)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    # Without this, browsers can serve a stale /static/* file (site.css in
    # particular) on a plain reload - only a hard refresh forces a
    # re-fetch, since StaticFiles sends Last-Modified/ETag but no
    # Cache-Control, leaving browsers free to use heuristic caching.
    # "no-cache" still revalidates via ETag rather than skipping the cache
    # entirely, so it stays cheap.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def require_login(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def index(request: Request):
    context = {
        "show_logout": True,
        "host": host_info(),
        "ap": ap_info(),
        "wireguard": wireguard_info(),
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get(
    "/ap-details", response_class=HTMLResponse, dependencies=[Depends(require_login)]
)
def ap_details(request: Request):
    return templates.TemplateResponse(
        request,
        "details.html",
        {
            "show_logout": True,
            "heading": "AP Details",
            "output": _run_script("view_currently_associated_clients.sh"),
        },
    )


@app.get(
    "/wireguard-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_login)],
)
def wireguard_details(request: Request):
    return templates.TemplateResponse(
        request,
        "details.html",
        {
            "show_logout": True,
            "heading": "WireGuard Details",
            "output": _run_script("view_wireguard_status.sh"),
        },
    )


@app.get("/manage", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def manage(request: Request):
    return templates.TemplateResponse(request, "manage.html", {})


@app.post(
    "/run/setup_ap", response_class=HTMLResponse, dependencies=[Depends(require_login)]
)
async def start_setup_ap(mode: str = Form(...), band: str = Form("5")):
    if mode not in ("dual", "uplink"):
        raise HTTPException(status_code=400, detail="invalid mode")
    if band not in ("5", "2.4"):
        raise HTTPException(status_code=400, detail="invalid band")
    args = [mode, band] if mode == "uplink" else [mode]
    job_id = await _start_job("setup_ap.sh", args)
    return _sse_output_fragment(job_id, "job-output")


@app.post(
    "/run/uplink_wifi",
    response_class=HTMLResponse,
    dependencies=[Depends(require_login)],
)
async def start_uplink_wifi(ssid: str = Form(...), password: str = Form("")):
    if not ssid.strip():
        raise HTTPException(status_code=400, detail="SSID required")
    args = [ssid, password] if password else [ssid]
    job_id = await _start_job("uplink_wifi.sh", args)
    return _sse_output_fragment(job_id, "job-output")


@app.get("/run/stream/{job_id}", dependencies=[Depends(require_login)])
async def stream_job(job_id: str):
    return StreamingResponse(_stream_job(job_id), media_type="text/event-stream")


# Gives the redirect response below time to reach the browser, and the
# /shutting-down page time to render, before the Pi actually goes down.
POWER_ACTION_DELAY_SECONDS = 5


def _schedule_power_action(*script_args: str) -> None:
    async def _run():
        await asyncio.sleep(POWER_ACTION_DELAY_SECONDS)
        await asyncio.create_subprocess_exec(
            str(SCRIPTS_DIR / "shutdown_pi.sh"), *script_args
        )

    asyncio.create_task(_run())


@app.post(
    "/shutdown", response_class=HTMLResponse, dependencies=[Depends(require_login)]
)
async def shutdown_pi(request: Request):
    # Logged out up front - the session is meaningless once the Pi is
    # going down, and clearing it now means /shutting-down's poll finds
    # a login page rather than a still-"authenticated" one once the
    # server comes back.
    request.session.clear()
    _schedule_power_action()
    return HTMLResponse(
        "", status_code=204, headers={"HX-Redirect": "/shutting-down?action=shutdown"}
    )


@app.post(
    "/restart", response_class=HTMLResponse, dependencies=[Depends(require_login)]
)
async def restart_pi(request: Request):
    request.session.clear()
    _schedule_power_action("reboot")
    return HTMLResponse(
        "", status_code=204, headers={"HX-Redirect": "/shutting-down?action=restart"}
    )


@app.get("/shutting-down", response_class=HTMLResponse)
def shutting_down(request: Request):
    action = request.query_params.get("action")
    return templates.TemplateResponse(
        request, "shutting_down.html", {"restarting": action == "restart"}
    )


@app.get("/healthz", response_class=HTMLResponse)
def healthz():
    # Unauthenticated on purpose - /shutting-down polls this to find out
    # when the server (and so the Pi) is back up, before there's any
    # session to be logged into again.
    return "ok"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, PSK):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Incorrect PassKey"},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return HTMLResponse("/login", status_code=204, headers={'HX-Redirect': '/login'})


if __name__ == "__main__":
    import uvicorn

    # Overridable for local dev (see dev_webapp.sh) - unset, these match
    # the prod defaults exactly (0.0.0.0:443, no reload). Passed as an
    # import string rather than the app object since uvicorn requires that
    # form for reload=True to work.
    uvicorn.run(
        "app:app",
        host=os.environ.get("WEBAPP_HOST", "0.0.0.0"),
        # 443, the default HTTPS port - not privileged for this process
        # despite running as pi_user, not root: mnet-ap-webapp.service
        # grants just CAP_NET_BIND_SERVICE (see setup_webapp.sh), rather
        # than needing to run as root for this alone.
        port=int(os.environ.get("WEBAPP_PORT", "443")),
        ssl_certfile=os.environ.get("WEBAPP_CERT", str(BASE_DIR / "cert.pem")),
        ssl_keyfile=os.environ.get("WEBAPP_KEY", str(BASE_DIR / "key.pem")),
        reload=os.environ.get("WEBAPP_RELOAD") == "1",
    )
