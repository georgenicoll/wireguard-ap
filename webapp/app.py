# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "jinja2",
#   "htpy",
# ]
# ///
import socket
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from htpy import dd, dl, dt

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/hostinfo", response_class=HTMLResponse)
def hostinfo():
    return str(
        dl[
            dt["Hostname"],
            dd[socket.gethostname()],
            dt["IP address"],
            dd[_local_ip()],
        ]
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
