# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "jinja2",
#   "htpy",
#   "itsdangerous",
#   "python-multipart",
# ]
# ///
import asyncio
import os
import secrets
import socket
import subprocess
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from htpy import dd, dl, dt
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
# Where the setup scripts live - one level up, alongside webapp/ itself
# (see main.tf's remote_dir).
SCRIPTS_DIR = BASE_DIR.parent

# Both sourced from wireguard-ap.env (EnvironmentFile= in the systemd unit -
# see setup_webapp.sh), the same file the shell scripts use, rather than
# being set separately here: SSID for display, PSK (the AP's own Wi-Fi
# password) doubling as this site's login password.
SSID = os.environ.get("SSID", "mnet-ap")
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


def require_login(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def _local_ip() -> str:
    # Doesn't actually send anything (UDP connect just picks a local route);
    # works even with no default route, since it falls back below.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "unknown"
    finally:
        s.close()


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"ssid": SSID, "show_logout": True}
    )


@app.get(
    "/api/hostinfo", response_class=HTMLResponse, dependencies=[Depends(require_login)]
)
def hostinfo():
    return str(
        dl[
            dt["Hostname"],
            dd[socket.gethostname()],
            dt["IP address"],
            dd[_local_ip()],
        ]
    )


def _run_script(name: str) -> str:
    # Both scripts self-elevate internally (sudo, NOPASSWD - see the
    # README's sudoers section) rather than being invoked with sudo here.
    try:
        result = subprocess.run(
            [str(SCRIPTS_DIR / name)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Failed to run {name}: {e}"
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
    # Both setup_ap.sh and uplink_wifi.sh already self-elevate internally
    # (sudo, NOPASSWD - see the README's sudoers section), same as
    # _run_script above.
    process = await asyncio.create_subprocess_exec(
        str(SCRIPTS_DIR / name),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    job_id = uuid.uuid4().hex
    _jobs[job_id] = process
    return job_id


async def _stream_job(job_id: str):
    process = _jobs.get(job_id)
    if process is None or process.stdout is None:
        yield "event: done\ndata: (no such job - already finished, or never started)\n\n"
        return
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        yield f"data: {line.decode(errors='replace').rstrip(chr(10))}\n\n"
    returncode = await process.wait()
    yield f"data: \n\ndata: [exit code {returncode}]\n\n"
    yield "event: done\ndata: \n\n"
    _jobs.pop(job_id, None)


@app.post("/run/setup_ap", dependencies=[Depends(require_login)])
async def start_setup_ap(mode: str = Form(...), band: str = Form("5")):
    if mode not in ("dual", "uplink"):
        raise HTTPException(status_code=400, detail="invalid mode")
    if band not in ("5", "2.4"):
        raise HTTPException(status_code=400, detail="invalid band")
    job_id = await _start_job("setup_ap.sh", [mode, band])
    return {"job_id": job_id}


@app.post("/run/uplink_wifi", dependencies=[Depends(require_login)])
async def start_uplink_wifi(ssid: str = Form(...), password: str = Form("")):
    if not ssid.strip():
        raise HTTPException(status_code=400, detail="SSID required")
    args = [ssid, password] if password else [ssid]
    job_id = await _start_job("uplink_wifi.sh", args)
    return {"job_id": job_id}


@app.get("/run/stream/{job_id}", dependencies=[Depends(require_login)])
async def stream_job(job_id: str):
    return StreamingResponse(_stream_job(job_id), media_type="text/event-stream")


@app.post("/shutdown", dependencies=[Depends(require_login)])
async def shutdown_pi():
    # Fire-and-forget: "systemctl poweroff" schedules the shutdown and
    # returns, but not so fast that this response is guaranteed to reach
    # the browser first if awaited directly - start it as a background
    # task instead, so the HTTP response always goes out first.
    async def _run():
        await asyncio.create_subprocess_exec(str(SCRIPTS_DIR / "shutdown_pi.sh"))

    asyncio.create_task(_run())
    return {"status": "shutting down"}


@app.get(
    "/manage", response_class=HTMLResponse, dependencies=[Depends(require_login)]
)
def manage(request: Request):
    return templates.TemplateResponse(
        request, "manage.html", {"ssid": SSID, "show_logout": True}
    )


@app.get(
    "/wireguard", response_class=HTMLResponse, dependencies=[Depends(require_login)]
)
def wireguard_status(request: Request):
    return templates.TemplateResponse(
        request,
        "output.html",
        {
            "ssid": SSID,
            "show_logout": True,
            "heading": "WireGuard status",
            "output": _run_script("view_wireguard_status.sh"),
        },
    )


@app.get("/clients", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def clients(request: Request):
    return templates.TemplateResponse(
        request,
        "output.html",
        {
            "ssid": SSID,
            "show_logout": True,
            "heading": "AP Status",
            "output": _run_script("view_currently_associated_clients.sh"),
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"ssid": SSID, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, PSK):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"ssid": SSID, "error": "Incorrect password"},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        # 443, the default HTTPS port - not privileged for this process
        # despite running as pi_user, not root: mnet-ap-webapp.service
        # grants just CAP_NET_BIND_SERVICE (see setup_webapp.sh), rather
        # than needing to run as root for this alone.
        port=443,
        ssl_certfile=str(BASE_DIR / "cert.pem"),
        ssl_keyfile=str(BASE_DIR / "key.pem"),
    )
