"""Анонимный веб-чат для гостей Free Wi-Fi с мостом в Telegram-форум.

Каждый гость (по случайному токену в cookie) получает номер «Анон N» и свою тему
в Telegram-группе-форуме. Ответ в теме уходит обратно этому гостю.
"""
import asyncio
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

import info
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env(BASE / ".env")
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = int(os.environ["TG_CHAT_ID"])
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
# для привязки к устройству: точный uamserver из Keenetic и его uamsecret
PORTAL_URL = os.environ.get("PORTAL_URL", "")
UAM_SECRET = os.environ.get("UAM_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", str(BASE / "chat.db"))
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

MAX_TEXT = 1000
MAX_NAME = 32
COOKIE = "sid"
SID_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
MAC_RE = re.compile(r"^[0-9A-F]{2}(-[0-9A-F]{2}){5}$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")

# ---------- база ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS guests(
  sid TEXT PRIMARY KEY, num INTEGER UNIQUE, name TEXT, topic_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, num INTEGER, out INTEGER, text TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS devices(mac TEXT PRIMARY KEY, sid TEXT, seen REAL);
""")
db_lock = asyncio.Lock()


def q(sql, args=()):
    return db.execute(sql, args).fetchall()


def clean(s: str, limit: int) -> str:
    return CTRL_RE.sub("", s).strip()[:limit]


# ---------- rate limit (в памяти) ----------
class Limiter:
    def __init__(self, n: int, per: float):
        self.n, self.per, self.hits = n, per, defaultdict(deque)

    def ok(self, key: str) -> bool:
        now, d = time.monotonic(), self.hits[key]
        while d and now - d[0] > self.per:
            d.popleft()
        if len(d) >= self.n:
            return False
        d.append(now)
        return True


per_guest = Limiter(8, 60)       # 8 сообщений в минуту на гостя
per_guest_burst = Limiter(1, 1.5)  # не чаще раза в 1.5 c
global_lim = Limiter(120, 60)    # все гости вместе (за NAT у них общий IP)
new_guests = Limiter(30, 3600)   # новых тем в час


# ---------- telegram ----------
http: httpx.AsyncClient | None = None


async def tg(method: str, **params):
    r = await http.post(f"{TG_API}/{method}", json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data.get('description')}")
    return data["result"]


def topic_title(num: int, name: str | None) -> str:
    return f"Анон{num}" + (f" · {name}" if name else "")


async def ensure_topic(sid: str) -> tuple[int, int]:
    """Возвращает (num, topic_id), создавая гостя/тему при необходимости."""
    async with db_lock:
        row = q("SELECT num, topic_id, name FROM guests WHERE sid=?", (sid,))
        if row and row[0][1]:
            return row[0][0], row[0][1]
        if not row:
            if not new_guests.ok("all"):
                raise HTTPException(429, "Слишком много новых гостей, попробуйте позже")
            num = (q("SELECT COALESCE(MAX(num),0)+1 FROM guests")[0][0])
            q("INSERT INTO guests(sid,num,created) VALUES(?,?,?)", (sid, num, time.time()))
            name = None
        else:
            num, name = row[0][0], row[0][2]
        topic = await tg("createForumTopic", chat_id=TG_CHAT_ID, name=topic_title(num, name))
        q("UPDATE guests SET topic_id=? WHERE sid=?", (topic["message_thread_id"], sid))
        return num, topic["message_thread_id"]


async def poll_telegram():
    """Забирает ответы из тем группы и кладёт их в историю гостя."""
    offset = int((q("SELECT v FROM kv WHERE k='offset'") or [["0"]])[0][0])
    while True:
        try:
            updates = await tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
            for u in updates:
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if m.get("chat", {}).get("id") != TG_CHAT_ID:
                    continue
                if m.get("from", {}).get("is_bot") or not m.get("is_topic_message"):
                    continue
                text = m.get("text") or m.get("caption")
                if not text:
                    continue
                row = q("SELECT num FROM guests WHERE topic_id=?", (m["message_thread_id"],))
                if row:
                    q("INSERT INTO messages(num,out,text,ts) VALUES(?,1,?,?)",
                      (row[0][0], clean(text, 4000), time.time()))
            q("INSERT OR REPLACE INTO kv VALUES('offset',?)", (str(offset),))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # сеть/telegram — подождать и продолжить
            print("poll error:", e)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    http = httpx.AsyncClient()
    task = asyncio.create_task(poll_telegram())
    yield
    task.cancel()
    await http.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if int(request.headers.get("content-length") or 0) > 8192:
        return JSONResponse({"detail": "too large"}, status_code=413)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def get_sid(request: Request) -> str | None:
    sid = request.cookies.get(COOKIE)
    return sid if sid and SID_RE.match(sid) else None


def require_sid(request: Request) -> str:
    sid = get_sid(request)
    if not sid:
        raise HTTPException(401, "no session")
    # защита от CSRF: запросы только со своей страницы
    if request.headers.get("x-requested-with") != "chat":
        raise HTTPException(403, "forbidden")
    return sid


def signed_mac(request: Request) -> str | None:
    """MAC устройства из редиректа Keenetic/Chilli, если подпись md верна."""
    if not (PORTAL_URL and UAM_SECRET):
        return None
    raw = request.scope.get("query_string", b"").decode("latin-1")
    base, sep, md = raw.rpartition("&md=")
    if not sep or len(md) != 32:
        return None
    want = hashlib.md5((PORTAL_URL + "?" + base + UAM_SECRET).encode("latin-1")).hexdigest().upper()
    if not hmac.compare_digest(want, md.upper()):
        return None
    mac = (request.query_params.get("mac") or "").upper()
    return mac if MAC_RE.match(mac) else None


def with_cookie(resp: Response, sid: str) -> Response:
    resp.set_cookie(COOKIE, sid, max_age=30 * 86400, httponly=True,
                    samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.get("/")
async def index(request: Request):
    sid = get_sid(request)
    mac = signed_mac(request)
    if mac:
        # одно устройство = один гость, даже в другом окне/браузере
        row = q("SELECT sid FROM devices WHERE mac=?", (mac,))
        if row:
            sid = row[0][0]
        else:
            sid = sid or secrets.token_urlsafe(32)
            q("INSERT INTO devices(mac,sid,seen) VALUES(?,?,?)", (mac, sid, time.time()))
        q("UPDATE devices SET seen=? WHERE mac=?", (time.time(), mac))
        # убираем подписанные параметры из адресной строки
        return with_cookie(RedirectResponse("/", status_code=303), sid)
    resp = FileResponse(BASE / "static" / "index.html")
    return resp if sid else with_cookie(resp, secrets.token_urlsafe(32))


@app.get("/api/info")
async def get_info():
    return await info.collect(http)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/static/{name}")
async def static(name: str):
    if name not in {"chat.js", "chat.css", "info.js"}:
        raise HTTPException(404)
    return FileResponse(BASE / "static" / name)


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT * 2)
    name: str | None = Field(default=None, max_length=MAX_NAME * 2)


@app.get("/api/me")
async def me(request: Request):
    sid = require_sid(request)
    row = q("SELECT num, name FROM guests WHERE sid=?", (sid,))
    return {"num": row[0][0] if row else None, "name": row[0][1] if row else None}


@app.post("/api/send")
async def send(body: SendIn, request: Request):
    sid = require_sid(request)
    text = clean(body.text, MAX_TEXT)
    name = clean(body.name or "", MAX_NAME) or None
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if not (per_guest_burst.ok(sid) and per_guest.ok(sid) and global_lim.ok("all")):
        raise HTTPException(429, "Слишком часто, подождите немного")

    num, topic_id = await ensure_topic(sid)
    old = q("SELECT name FROM guests WHERE sid=?", (sid,))[0][0]
    if name != old and name is not None:
        q("UPDATE guests SET name=? WHERE sid=?", (name, sid))
        try:
            await tg("editForumTopic", chat_id=TG_CHAT_ID, message_thread_id=topic_id,
                     name=topic_title(num, name))
        except Exception as e:
            print("rename error:", e)
    try:
        await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=topic_id, text=text,
                 link_preview_options={"is_disabled": True})
    except RuntimeError as e:
        if "thread not found" in str(e).lower():  # тему удалили в TG — создать заново
            q("UPDATE guests SET topic_id=NULL WHERE sid=?", (sid,))
            num, topic_id = await ensure_topic(sid)
            await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=topic_id, text=text)
        else:
            raise HTTPException(502, "Не удалось отправить, попробуйте ещё раз")
    q("INSERT INTO messages(num,out,text,ts) VALUES(?,0,?,?)", (num, text, time.time()))
    return {"ok": True, "num": num}


@app.get("/api/messages")
async def messages(request: Request, after: int = 0):
    sid = require_sid(request)
    row = q("SELECT num FROM guests WHERE sid=?", (sid,))
    if not row:
        return {"messages": []}
    rows = q("SELECT id, out, text, ts FROM messages WHERE num=? AND id>? ORDER BY id LIMIT 200",
             (row[0][0], max(after, 0)))
    return {"messages": [{"id": i, "out": bool(o), "text": t, "ts": ts} for i, o, t, ts in rows]}


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return Response(status_code=404)
