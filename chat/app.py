"""Анонимный веб-чат для гостей Free Wi-Fi с мостом в Telegram-форум.

Каждый гость (по случайному токену в cookie) получает номер «Анон N» и свою тему
в Telegram-группе-форуме. Ответ в теме уходит обратно этому гостю.
"""
import asyncio
import hashlib
import hmac
import ipaddress
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
import visits
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
CREATE TABLE IF NOT EXISTS room(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT, num INTEGER, name TEXT, text TEXT, ts REAL,
  deleted INTEGER DEFAULT 0, tg_id INTEGER);
CREATE TABLE IF NOT EXISTS room_names(sid TEXT PRIMARY KEY, display TEXT UNIQUE COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS bans(sid TEXT PRIMARY KEY, ts REAL);
""")
db.executescript(visits.SCHEMA)
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
        if len(self.hits) > 20000:  # защита памяти от множества ключей (сессий/IP)
            self.hits.clear()
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


NEWS_MAX = 500


async def set_news(m: dict, text: str) -> None:
    """/news текст — объявление на странице; /news без текста — убрать."""
    body = clean(text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else "", NEWS_MAX)
    if body:
        q("INSERT OR REPLACE INTO kv VALUES('news',?)", (body,))
        reply = "📢 Объявление на странице обновлено"
    else:
        q("DELETE FROM kv WHERE k='news'")
        reply = "📢 Объявление убрано"
    try:
        await tg("sendMessage", chat_id=TG_CHAT_ID, text=reply, reply_to_message_id=m["message_id"],
                 **({"message_thread_id": m["message_thread_id"]} if m.get("is_topic_message") else {}))
    except Exception as e:
        print("news reply error:", e)


# ---------- общий чат: фильтры и модерация ----------
ROOM_MAX = 500
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|\b[\w-]+\.(com|ru|tj|net|org|me|io|uz|kz|info|xyz|site|online)\b)", re.I)
BAD_RE = re.compile(
    r"\b\w*(?:пизд|х[уy][йеёяию]|муда[кч]|пид[оа]р|гандон|шлюх|залуп)\w*"
    r"|\b(?:за|на|вы|у|по|до|от|раз|с|въ|отъ|съ|подъ|при)?[её]б(?:а|у|л|ы|н|и|ё|о|т)\w*"
    r"|\bбл[яе]т?ь?\b|\bблят\w*|\bсук[аи]\b|\bсучк\w*", re.I)


def censor(s: str) -> str:
    return BAD_RE.sub(lambda m: m.group(0)[0] + "***", s)


async def room_topic() -> int:
    """Тема «👥 Общий чат» в группе — туда копируются все сообщения комнаты."""
    row = q("SELECT v FROM kv WHERE k='room_topic'")
    if row:
        return int(row[0][0])
    async with db_lock:
        row = q("SELECT v FROM kv WHERE k='room_topic'")
        if row:
            return int(row[0][0])
        t = await tg("createForumTopic", chat_id=TG_CHAT_ID, name="👥 Общий чат")
        q("INSERT OR REPLACE INTO kv VALUES('room_topic',?)", (str(t["message_thread_id"]),))
        await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=t["message_thread_id"], text=(
            "Сюда копируется общий чат со страницы.\n"
            "Ответ в этой теме — сообщение «👑 Админ» в общий чат.\n"
            "/del 12 — удалить сообщение #12\n/ban 12 — забанить автора #12 и удалить все его сообщения\n"
            "/unban 12 — разбанить\n/clear — очистить общий чат"))
        return t["message_thread_id"]


async def moderate(m: dict, text: str) -> None:
    parts = text.split()
    cmd = parts[0].split("@")[0][1:]
    arg = parts[1].lstrip("#") if len(parts) > 1 else ""
    reply = "Формат: /del 12, /ban 12, /unban 12, /clear"
    if cmd == "clear":
        q("UPDATE room SET deleted=1")
        reply = "🧹 Общий чат очищен"
    elif arg.isdigit():
        row = q("SELECT sid, name FROM room WHERE id=?", (int(arg),))
        if not row:
            reply = f"Сообщения #{arg} нет"
        elif cmd == "del":
            q("UPDATE room SET deleted=1 WHERE id=?", (int(arg),))
            reply = f"🗑 Сообщение #{arg} удалено"
        elif row[0][0] == "admin":
            reply = "Это сообщение админа"
        elif cmd == "ban":
            q("INSERT OR REPLACE INTO bans VALUES(?,?)", (row[0][0], time.time()))
            n = q("SELECT COUNT(*) FROM room WHERE sid=? AND deleted=0", (row[0][0],))[0][0]
            q("UPDATE room SET deleted=1 WHERE sid=?", (row[0][0],))
            reply = f"🚫 {row[0][1]} забанен, удалено сообщений: {n}"
        elif cmd == "unban":
            q("DELETE FROM bans WHERE sid=?", (row[0][0],))
            reply = f"✅ {row[0][1]} разбанен"
    try:
        await tg("sendMessage", chat_id=TG_CHAT_ID, text=reply, reply_to_message_id=m["message_id"],
                 **({"message_thread_id": m["message_thread_id"]} if m.get("is_topic_message") else {}))
    except Exception as e:
        print("moderate reply error:", e)


# ---------- журнал посещений ----------
visit_notify = Limiter(20, 60)  # не больше 20 уведомлений в минуту в тему «Посетители»


async def visits_topic() -> int:
    row = q("SELECT v FROM kv WHERE k='visits_topic'")
    if row:
        return int(row[0][0])
    async with db_lock:
        row = q("SELECT v FROM kv WHERE k='visits_topic'")
        if row:
            return int(row[0][0])
        t = await tg("createForumTopic", chat_id=TG_CHAT_ID, name="📊 Посетители")
        q("INSERT OR REPLACE INTO kv VALUES('visits_topic',?)", (str(t["message_thread_id"]),))
        await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=t["message_thread_id"], text=(
            "Сюда пишется каждый новый заход на страницу (повтор в течение 30 мин — та же запись).\n"
            "/visits — последние 15 заходов (/visits 40 — больше)\n/stats — сводка: сколько, марки, браузеры\n"
            "/who a1b2c3 — все заходы устройства с этим 🆔 (+ полный User-Agent)"))
        return t["message_thread_id"]


async def notify_visit(vid: int) -> None:
    await asyncio.sleep(6)  # ждём уточнения от страницы (модель, экран)
    try:
        r = visits.rows(q, "v.id=? AND v.sent=0", (vid,), 1)
        if not r or not visit_notify.ok("all"):
            return
        q("UPDATE visits SET sent=1 WHERE id=?", (vid,))
        before = q("SELECT COUNT(*) FROM visits WHERE sid=(SELECT sid FROM visits WHERE id=?) AND id<?", (vid, vid))[0][0]
        head = "🆕 Новое устройство" if not before else f"🔁 Снова зашёл (визит {before + 1})"
        await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=await visits_topic(),
                 text=head + "\n" + visits.line(r[0]), disable_notification=True)
    except Exception as e:
        print("visit notify error:", e)


async def visits_cmd(m: dict, text: str) -> None:
    parts = text.split()
    cmd = parts[0].split("@")[0][1:]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "stats":
        msgs = [visits.stats(q)]
    elif cmd == "who":
        rs = visits.rows(q, "v.dev=?", (arg.lower(),), 10) if re.fullmatch(r"[0-9a-fA-F]{6}", arg) else []
        msgs = [f"Заходов устройства {arg}: {len(rs)} (последние)"] + [visits.line(r, full=(i == 0)) for i, r in enumerate(rs)] \
            if rs else ["Формат: /who a1b2c3 (🆔 из журнала)"]
    else:
        n = min(int(arg), 50) if arg.isdigit() else 15
        rs = visits.rows(q, limit=n)
        msgs = [f"Последние заходы: {len(rs)}"] + [visits.line(r) for r in reversed(rs)]
    chunk, out = "", []
    for x in msgs:  # лимит Telegram — 4096 символов
        if len(chunk) + len(x) > 3800:
            out.append(chunk); chunk = ""
        chunk += x + "\n\n"
    out.append(chunk)
    thread = {"message_thread_id": m["message_thread_id"]} if m.get("is_topic_message") else {}
    for c in out:
        try:
            await tg("sendMessage", chat_id=TG_CHAT_ID, text=c.strip(), **thread)
        except Exception as e:
            print("visits cmd error:", e)


def guest_num(sid: str) -> int:
    row = q("SELECT num FROM guests WHERE sid=?", (sid,))
    if row:
        return row[0][0]
    if not new_guests.ok("all"):
        raise HTTPException(429, "Слишком много новых гостей, попробуйте позже")
    num = q("SELECT COALESCE(MAX(num),0)+1 FROM guests")[0][0]
    q("INSERT INTO guests(sid,num,created) VALUES(?,?,?)", (sid, num, time.time()))
    return num


def display_name(sid: str, num: int, name: str | None) -> str:
    """Имя в общем чате: занятое другим гостем имя получает номер — «Али 2»."""
    want = name or f"Анон{num}"
    if want.lower() in ("админ", "admin", "👑 админ", "администратор"):
        want = f"Анон{num}"
    cur = q("SELECT display FROM room_names WHERE sid=?", (sid,))
    if cur and (cur[0][0] == want or re.fullmatch(re.escape(want) + r" \d+", cur[0][0], re.I)):
        return cur[0][0]
    cand, k = want, 1
    while q("SELECT 1 FROM room_names WHERE display=? AND sid<>?", (cand, sid)):
        k += 1
        cand = f"{want} {k}"
    q("INSERT OR REPLACE INTO room_names VALUES(?,?)", (sid, cand))
    return cand


ONLINE_BASE = int(os.environ.get("ONLINE_BASE", "4"))  # прибавка к счётчику «на сайте»

# кто сейчас на странице: sid -> время последнего опроса
online: dict[str, float] = {}


def online_count() -> int:
    edge = time.time() - 90
    for k in [k for k, v in online.items() if v < edge]:
        online.pop(k, None)
    return len(online)


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
                if m.get("from", {}).get("is_bot"):
                    continue
                text = m.get("text") or m.get("caption")
                if text and re.match(r"^/news(@\w+)?(\s|$)", text):
                    await set_news(m, text)
                    continue
                if text and re.match(r"^/(visits|stats|who)(@\w+)?(\s|$)", text):
                    await visits_cmd(m, text)
                    continue
                if text and re.match(r"^/(del|ban|unban|clear)(@\w+)?(\s|$)", text):
                    await moderate(m, text)
                    continue
                if (m.get("is_topic_message") and text
                        and str(m["message_thread_id"]) == (q("SELECT v FROM kv WHERE k='room_topic'") or [[""]])[0][0]):
                    q("INSERT INTO room(sid,num,name,text,ts) VALUES('admin',0,'👑 Админ',?,?)",
                      (clean(text, ROOM_MAX), time.time()))
                    continue
                if not m.get("is_topic_message"):
                    continue
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


def cache_key() -> str:
    return hmac.new(UAM_SECRET.encode(), b"cache-export", hashlib.sha256).hexdigest()


async def take_over_cache():
    """При деплое старая версия ещё отвечает по публичному адресу — забираем у неё готовые данные.
    Внешние сервисы тогда опрашиваются только в HH:00, а не при каждом деплое."""
    if not (UAM_SECRET and PORTAL_URL):
        return
    await asyncio.sleep(1)
    try:
        r = await http.get(PORTAL_URL.rstrip("/") + "/api/cache-export",
                           headers={"X-Cache-Key": cache_key()}, timeout=25)
        if r.status_code == 200:
            d = r.json()
            print("cache taken over:", info.import_state(d), "sources,", import_db(d.get("db")), "db rows")
        else:
            print("cache take-over: HTTP", r.status_code)
    except Exception as e:
        print("cache take-over failed:", type(e).__name__)  # обычный холодный старт: данные подтянет первый гость


async def refresh_hourly():
    """Единственное место, где сервер ходит во внешние сервисы.
    Раз в минуту проверяет кеш: устаревшее (срок — ровно HH:00) обновляется, остальное не трогается.
    После неудачи источник повторяется через минуту (см. info.cached)."""
    await asyncio.sleep(8)  # сначала дать шанс забрать кеш у предыдущей версии
    while True:
        try:
            await info.refresh(http)
        except Exception as e:
            print("refresh error:", e)
        await asyncio.sleep(60 - time.time() % 60 + 5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    http = httpx.AsyncClient()
    task = asyncio.create_task(poll_telegram())
    asyncio.create_task(take_over_cache())  # кеш от предыдущей версии — без лишних запросов к сервисам
    hourly = asyncio.create_task(refresh_hourly())
    yield
    task.cancel()
    hourly.cancel()
    await http.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def client_ip(request: Request) -> str:
    """Реальный IP клиента. Перед Render стоит Cloudflare: он сам ставит CF-Connecting-IP
    (значение от клиента перезаписывается). Запасной вариант — самый правый публичный адрес в XFF."""
    cf = request.headers.get("cf-connecting-ip", "").strip()
    try:
        return str(ipaddress.ip_address(cf))
    except ValueError:
        pass
    for part in reversed(request.headers.get("x-forwarded-for", "").split(",")):
        try:
            ip = ipaddress.ip_address(part.strip())
        except ValueError:
            continue
        if ip.is_global:
            return str(ip)
    return (request.client.host if request.client else "?")[:64]


# внешний IP нашей точки Wi-Fi (все гости за NAT роутера выходят с него)
WIFI_IPS = {x.strip() for x in os.environ.get("WIFI_IPS", "").split(",") if x.strip()}


def from_wifi(request: Request, sid: str) -> bool:
    if q("SELECT 1 FROM devices WHERE sid=?", (sid,)):
        q("INSERT OR REPLACE INTO kv VALUES('wifi_ip',?)", (client_ip(request),))  # уточнить IP точки
        return True
    ip = client_ip(request)
    if ip in WIFI_IPS:
        return True
    row = q("SELECT v FROM kv WHERE k='wifi_ip'")
    return bool(row) and row[0][0] == ip


ip_lim = Limiter(1200, 60)  # все запросы с одного IP в минуту (гости Wi-Fi за одним IP, опрос раз в 3 c)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.url.path != "/healthz" and not ip_lim.ok(client_ip(request)):
        return JSONResponse({"detail": "Слишком много запросов"}, status_code=429)
    cl = request.headers.get("content-length")
    if request.method == "POST" and (cl is None or not cl.isdigit()):
        return JSONResponse({"detail": "length required"}, status_code=411)
    if int(cl or 0) > 8192:
        return JSONResponse({"detail": "too large"}, status_code=413)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers.setdefault("Cache-Control", "no-store")
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
        q("INSERT OR REPLACE INTO kv VALUES('wifi_ip',?)", (client_ip(request),))  # запомнить IP точки
        # убираем подписанные параметры из адресной строки
        return with_cookie(RedirectResponse("/", status_code=303), sid)
    resp = FileResponse(BASE / "static" / "index.html")
    if not sid:
        sid = secrets.token_urlsafe(32)
        with_cookie(resp, sid)
    log_visit(request, sid)
    # просим Chrome прислать точную модель телефона (без этого в UA часто просто «K»)
    resp.headers["Accept-CH"] = "Sec-CH-UA-Model, Sec-CH-UA-Platform-Version"
    return resp


def log_visit(request: Request, sid: str) -> None:
    try:
        h = request.headers
        dev = q("SELECT mac FROM devices WHERE sid=?", (sid,))
        ua = h.get("user-agent", "")
        model = h.get("sec-ch-ua-model", "").strip('" ')
        vid, new = visits.record(q, sid, client_ip(request), ua, h.get("accept-language", "").split(",")[0],
                                 h.get("referer", ""), dev[0][0] if dev else None, from_wifi(request, sid))
        if model or h.get("sec-ch-ua-platform-version"):
            visits.hello(q, sid, {"model": model, "pv": h.get("sec-ch-ua-platform-version", "").strip('" ')})
        if new:
            asyncio.create_task(notify_visit(vid))
    except Exception as e:  # журнал не должен ломать страницу
        print("visit log error:", e)


class HelloIn(BaseModel):
    screen: str = Field("", max_length=30)
    tz: str = Field("", max_length=40)
    lang: str = Field("", max_length=40)
    model: str = Field("", max_length=60)
    pv: str = Field("", max_length=20)


hello_lim = Limiter(10, 60)


@app.post("/api/hello")
async def hello(body: HelloIn, request: Request):
    sid = require_sid(request)
    if hello_lim.ok(sid):
        visits.hello(q, sid, {k: clean(v, 60) for k, v in body.model_dump().items()})
    return {"ok": True}


def require_page(request: Request) -> None:
    """Данные — только своей странице: нужна cookie сессии и заголовок от нашего JS."""
    if not get_sid(request) or request.headers.get("x-requested-with") != "chat":
        raise HTTPException(404)


@app.get("/api/info")
async def get_info(request: Request):
    require_page(request)
    data = await info.collect()  # только кеш — запросы гостей никогда не дёргают внешние сервисы
    news = q("SELECT v FROM kv WHERE k='news'")
    data["news"] = news[0][0] if news else None
    data["online"] = ONLINE_BASE + online_count()
    return data


@app.get("/api/diag-ip")
async def diag_ip(request: Request):
    """Диагностика: какие адресные заголовки приходят через прокси Render (только по ключу)."""
    if not (UAM_SECRET and hmac.compare_digest(request.headers.get("x-cache-key", ""), cache_key())):
        raise HTTPException(404)
    h = request.headers
    return {"xff": h.get("x-forwarded-for"), "cf": h.get("cf-connecting-ip"), "true": h.get("true-client-ip"),
            "real": h.get("x-real-ip"), "client": request.client.host if request.client else None,
            "computed": client_ip(request), "wifi_ip": (q("SELECT v FROM kv WHERE k='wifi_ip'") or [[None]])[0][0],
            "wifi_ips_env": sorted(WIFI_IPS)}


DB_EXPORT = {  # таблица -> сколько последних строк передавать новой версии
    "guests": 100000, "devices": 100000, "bans": 100000, "room_names": 100000, "kv": 1000,
    "room": 1000, "messages": 5000, "visits": 20000,
}


@app.get("/api/cache-export")
async def cache_export(request: Request):
    key = request.headers.get("x-cache-key", "")
    if not (UAM_SECRET and hmac.compare_digest(key, cache_key())):
        raise HTTPException(404)
    data = info.export_state()
    tables = {}
    for tbl, lim in DB_EXPORT.items():
        cur = db.execute(f"SELECT * FROM (SELECT rowid AS _r, * FROM {tbl} ORDER BY rowid DESC LIMIT ?) ORDER BY _r", (lim,))
        cols = [c[0] for c in cur.description][1:]
        tables[tbl] = {"cols": cols, "rows": [list(r[1:]) for r in cur.fetchall()]}
    data["db"] = tables
    return data


def import_db(tables: dict) -> int:
    n = 0
    for tbl, d in (tables or {}).items():
        if tbl not in DB_EXPORT:
            continue
        cols = [c for c in d.get("cols", []) if re.fullmatch(r"[a-z_]+", c)]
        if not cols or len(cols) != len(d["cols"]):
            continue
        sql = f"INSERT OR IGNORE INTO {tbl}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        for r in d.get("rows", []):
            if len(r) == len(cols):
                db.execute(sql, r); n += 1
    return n


IMG_ID_RE = re.compile(r"^[0-9a-f]{16}$")


@app.get("/media/{iid}")
async def media(request: Request, iid: str, dl: int = 0):
    item = info.images.get(iid) if IMG_ID_RE.match(iid) and get_sid(request) else None
    if not item:
        raise HTTPException(404)
    headers = {"Cache-Control": "public, max-age=86400"}
    if dl:
        ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(item[1], "jpg")
        headers["Content-Disposition"] = f'attachment; filename="foto-dnya-{iid[:6]}.{ext}"'
    return Response(item[0], media_type=item[1], headers=headers)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/static/{name}")
async def static(name: str):
    if name not in {"chat.js", "chat.css", "info.js", "game.js"}:
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
    if len(online) < 10000:
        online[sid] = time.time()
    row = q("SELECT num FROM guests WHERE sid=?", (sid,))
    if not row:
        return {"messages": []}
    rows = q("SELECT id, out, text, ts FROM messages WHERE num=? AND id>? ORDER BY id LIMIT 200",
             (row[0][0], max(after, 0)))
    return {"messages": [{"id": i, "out": bool(o), "text": t, "ts": ts} for i, o, t, ts in rows]}


room_guest = Limiter(6, 60)
room_burst = Limiter(1, 2)
room_ip = Limiter(30, 60)          # сообщений в общий чат с одного IP (у гостей Wi-Fi он общий)
# писать в общий чат — только устройствам, пришедшим через роутер (подписанный MAC)
ROOM_WIFI_ONLY = os.environ.get("ROOM_WIFI_ONLY", "1" if UAM_SECRET else "0") == "1"
ROOM_KEEP = 1000


def room_you(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()[:10]


@app.get("/api/room")
async def room_get(request: Request, after: int = 0):
    sid = require_sid(request)
    if len(online) < 10000:
        online[sid] = time.time()
    rows = q("SELECT id, sid, num, name, text, ts FROM room WHERE id>? AND deleted=0 ORDER BY id DESC LIMIT 100",
             (max(after, 0),))
    # удалённые модератором — чтобы убрать их с экранов
    gone = [r[0] for r in q("SELECT id FROM room WHERE deleted=1 ORDER BY id DESC LIMIT 300")]
    return {"src": "cloud", "you": room_you(sid), "deleted": gone,
            "messages": [{"id": i, "u": room_you(s), "num": n, "name": nm, "text": tx, "ts": ts}
                         for i, s, n, nm, tx, ts in reversed(rows)]}


@app.post("/api/room")
async def room_post(body: SendIn, request: Request):
    sid = require_sid(request)
    if q("SELECT 1 FROM bans WHERE sid=?", (sid,)):
        raise HTTPException(403, "Вам запрещено писать в общий чат")
    if ROOM_WIFI_ONLY and not from_wifi(request, sid):
        raise HTTPException(403, "Писать в общий чат можно только подключившись к Free Wifi")
    text = clean(body.text, ROOM_MAX)
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if LINK_RE.search(text):
        raise HTTPException(400, "Ссылки в общем чате запрещены")
    if not (room_burst.ok(sid) and room_guest.ok(sid) and room_ip.ok(client_ip(request)) and global_lim.ok("all")):
        raise HTTPException(429, "Не так быстро 🙂")
    num = guest_num(sid)
    name = display_name(sid, num, clean(body.name or "", MAX_NAME) or None)
    text = censor(text)
    cur = db.execute("INSERT INTO room(sid,num,name,text,ts) VALUES(?,?,?,?,?)", (sid, num, name, text, time.time()))
    mid = cur.lastrowid
    q("DELETE FROM room WHERE id <= ?", (mid - ROOM_KEEP,))
    try:
        topic = await room_topic()
        await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=topic, text=f"#{mid} {name} (Анон{num}): {text}",
                 link_preview_options={"is_disabled": True})
    except Exception as e:
        print("room copy error:", e)
    return {"ok": True, "id": mid, "name": name}


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return Response(status_code=404)
