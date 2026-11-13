import hashlib
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime

app = FastAPI(title="Captive Portal")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

UAM_SECRET = os.environ.get("UAM_SECRET", "")  # задаётся в .env, должен совпадать с UAM Secret в роутере


def uam_password(challenge: str) -> str:
    """Считает UAM-пароль для входа без пароля (пустой пароль)."""
    md5_empty = hashlib.md5(b"").digest()
    challenge_bytes = bytes.fromhex(challenge)
    return hashlib.md5(challenge_bytes + md5_empty).hexdigest()


@app.get("/", response_class=HTMLResponse)
async def portal(request: Request):
    # Параметры, которые роутер передаёт при перенаправлении
    uamip    = request.query_params.get("uamip", "")
    uamport  = request.query_params.get("uamport", "3990")
    challenge = request.query_params.get("challenge", "")
    mac      = request.query_params.get("mac", "")
    ip       = request.query_params.get("ip", "")
    userurl  = request.query_params.get("userurl", "http://google.com")

    # Строим URL для авторизации через UAM
    login_url = ""
    if uamip and challenge:
        password = uam_password(challenge)
        login_url = (
            f"http://{uamip}:{uamport}/logon"
            f"?username={mac}"
            f"&password={password}"
            f"&userurl={userurl}"
        )

    now = datetime.now().strftime("%d %B %Y, %H:%M")

    return templates.TemplateResponse(request, "index.html", {
        "time":      now,
        "login_url": login_url,
        "mac":       mac,
        "ip":        ip,
        "has_uam":   bool(uamip and challenge),
    })


@app.get("/api/ping")
async def ping():
    return {"status": "ok", "message": "pong"}