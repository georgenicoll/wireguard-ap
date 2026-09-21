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
import os
import secrets
import socket
import subprocess
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
    return templates.TemplateResponse(request, "index.html", {"ssid": SSID})


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


@app.get(
    "/wireguard", response_class=HTMLResponse, dependencies=[Depends(require_login)]
)
def wireguard_status(request: Request):
    return templates.TemplateResponse(
        request,
        "output.html",
        {
            "ssid": SSID,
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
            "heading": "Connected clients",
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
        port=8000,
        ssl_certfile=str(BASE_DIR / "cert.pem"),
        ssl_keyfile=str(BASE_DIR / "key.pem"),
    )
