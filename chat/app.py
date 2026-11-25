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

import access
import backup
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
-- один человек = один гость: признаки (sid:/mac:/user:/fp:) -> главный sid этого гостя
CREATE TABLE IF NOT EXISTS guest_keys(key TEXT PRIMARY KEY, sid TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS ads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, contact TEXT, created REAL, until REAL, off INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ad_stats(ad_id INTEGER, sid TEXT, day TEXT, click INTEGER DEFAULT 0, PRIMARY KEY(ad_id, sid, day));
""")
db.executescript(visits.SCHEMA)
db.executescript(access.SCHEMA)
# параметры Chilli из подписанного редиректа: куда отправить браузер для входа
db.execute("CREATE TABLE IF NOT EXISTS chilli_ctx(mac TEXT PRIMARY KEY, uamip TEXT, uamport INTEGER, challenge TEXT, ts REAL)")
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


async def tg_service(key: str, getter, **params):
    """Сообщение в служебную тему (её номер лежит в kv[key]). Если тему удалили в Telegram — завести новую."""
    try:
        return await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=await getter(), **params)
    except RuntimeError as e:
        if "thread not found" not in str(e).lower():
            raise
        q("DELETE FROM kv WHERE k=?", (key,))
        return await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=await getter(), **params)


# ---------- посетитель: один человек = один гость = одна тема ----------
# Объединяем гостей только по MAC из подписанного редиректа роутера: им не поделишься.
# Аккаунт (логином делятся) и отпечаток браузера (одинаковые телефоны совпадают) лишь подсказывают:
# в тему пишется «похоже на Анон N», но переписки не смешиваются.

def canon(sid: str) -> str:
    """Главный sid человека (все его браузеры/cookie сводятся к одному)."""
    row = q("SELECT sid FROM guest_keys WHERE key=?", ("sid:" + sid,))
    return row[0][0] if row else sid


def sids_of(g: str) -> list[str]:
    return [g] + [r[0][4:] for r in q("SELECT key FROM guest_keys WHERE sid=? AND key LIKE 'sid:%'", (g,))]


def person_model(g: str) -> str:
    ss = sids_of(g)
    r = q(f"SELECT brand, model, kind FROM visits WHERE sid IN ({','.join('?' * len(ss))}) "
          "AND kind<>'бот' ORDER BY id DESC LIMIT 1", ss)
    if not r:
        return ""
    brand, model, kind = r[0]
    return (model if model and brand and model.startswith(brand) else " ".join(x for x in (brand, model) if x)) or kind or ""


def guest_row(g: str):
    r = q("SELECT num, topic_id, name, created FROM guests WHERE sid=?", (g,))
    return r[0] if r else None


def hint(sid: str, key: str, what: str) -> None:
    """Слабый признак (аккаунт, отпечаток): запомнить первого владельца; совпал с другим — только подсказка."""
    me = canon(sid)
    row = q("SELECT sid FROM guest_keys WHERE key=?", (key,))
    if not row:
        q("INSERT INTO guest_keys VALUES(?,?,?)", (key, me, time.time()))
        return
    other = canon(row[0][0])
    if other == me:
        return
    pair = f"hint:{min(me, other)}:{max(me, other)}:{key.split(':')[0]}"
    if db.execute("INSERT OR IGNORE INTO guest_keys VALUES(?,?,?)", (pair, me, time.time())).rowcount:
        go = guest_row(other)
        say(me, f"ℹ️ {what} совпадает с {'Анон' + str(go[0]) if go else 'другим гостем'}. "
                "Может быть тот же человек, а может и нет — темы не объединены.")


def link(sid: str, key: str, how: str) -> str:
    """Сильный признак (MAC) принадлежит человеку с этим sid. Если признак уже чей-то — это один человек."""
    me = canon(sid)
    row = q("SELECT sid FROM guest_keys WHERE key=?", (key,))
    if not row:
        q("INSERT INTO guest_keys VALUES(?,?,?)", (key, me, time.time()))
        return me
    other = canon(row[0][0])
    return me if other == me else merge(me, other, how)


def merge(a: str, b: str, how: str) -> str:
    """Объединить двух гостей. Остаётся тот, у кого раньше появилась тема/номер (по умолчанию b — владелец признака)."""
    ga, gb = guest_row(a), guest_row(b)
    keep, drop = (a, b) if ga and (not gb or ga[3] < gb[3]) else (b, a)
    gk, gd = guest_row(keep), guest_row(drop)
    q("UPDATE guest_keys SET sid=? WHERE sid=? AND key NOT LIKE 'hint:%'", (keep, drop))
    if not q("SELECT 1 FROM room_names WHERE sid=?", (keep,)):  # ник в общем чате остаётся за человеком
        q("UPDATE OR IGNORE room_names SET sid=? WHERE sid=?", (keep, drop))
    q("INSERT OR REPLACE INTO guest_keys VALUES(?,?,?)", ("sid:" + drop, keep, time.time()))
    if gd:  # у обоих была своя тема: история переезжает, старая тема закрывается
        q("UPDATE messages SET num=? WHERE num=?", (gk[0], gd[0]))
        if gd[2] and not gk[2]:
            q("UPDATE guests SET name=? WHERE sid=?", (gd[2], keep))
        if q("SELECT 1 FROM bans WHERE sid=?", (drop,)):
            q("INSERT OR REPLACE INTO bans VALUES(?,?)", (keep, time.time()))
        q("DELETE FROM guests WHERE sid=?", (drop,))
        if gd[1]:  # ответы админа в закрытой теме — тоже этому человеку
            q("INSERT OR REPLACE INTO guest_keys VALUES(?,?,?)", (f"topic:{gd[1]}", keep, time.time()))
        say(keep, f"🔗 Это тот же человек, что Анон{gd[0]} (совпал {how}). Его тема закрыта, дальше всё здесь.")
        if gd[1]:
            spawn(close_topic(gd[1], gk[0]))
    elif gk:
        say(keep, f"🔗 Ещё один браузер/вход того же человека (узнан: {how})")
    return keep


async def close_topic(topic_id: int, num: int) -> None:
    try:
        await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=topic_id,
                 text=f"➡️ Это тот же человек, что Анон{num}. Дальше всё о нём — в его теме.")
        await tg("closeForumTopic", chat_id=TG_CHAT_ID, message_thread_id=topic_id)
    except Exception as e:
        print("close topic error:", e)


def topic_title(num: int, name: str | None, model: str = "") -> str:
    return (f"Анон{num}" + (f" · {name}" if name else (f" · {model}" if model else "")))[:128]


async def ensure_topic(sid: str) -> tuple[int, int]:
    """Возвращает (num, topic_id), создавая гостя/тему при необходимости. sid — уже главный (canon)."""
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
        topic = await tg("createForumTopic", chat_id=TG_CHAT_ID, name=topic_title(num, name, person_model(sid)))
        q("UPDATE guests SET topic_id=? WHERE sid=?", (topic["message_thread_id"], sid))
        return num, topic["message_thread_id"]


async def topic_send(g: str, text: str, **params) -> int:
    """Сообщение в тему гостя; тему удалили в Telegram — создать заново. Возвращает num гостя."""
    num, topic_id = await ensure_topic(g)
    try:
        await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=topic_id, text=text, **params)
    except RuntimeError as e:
        if "thread not found" not in str(e).lower():
            raise
        q("UPDATE guests SET topic_id=NULL WHERE sid=?", (g,))
        num, topic_id = await ensure_topic(g)
        await tg("sendMessage", chat_id=TG_CHAT_ID, message_thread_id=topic_id, text=text, **params)
    return num


# события о госте (заходы, имя, интернет, роутер) идут в его тему по очереди, в порядке появления
events: asyncio.Queue | None = None
guest_lim = Limiter(30, 60)  # не больше 30 событий в минуту на одного гостя
_bg: set = set()


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def say(sid: str | None, text: str, loud: bool = False) -> None:
    """Записать событие в тему гостя (не ждёт Telegram)."""
    if sid and events is not None and events.qsize() < 1000:
        events.put_nowait((sid, text, loud))


async def deliver(sid: str, text: str, loud: bool) -> None:
    g = canon(sid)  # мог объединиться, пока событие ждало очереди
    if not guest_lim.ok(g):
        return
    await topic_send(g, text, disable_notification=not loud, link_preview_options={"is_disabled": True})


async def events_loop():
    while True:
        sid, text, loud = await events.get()
        try:
            await deliver(sid, text, loud)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("guest event error:", e)


async def drain_events() -> None:
    """Для тестов: выполнить всё, что в очереди."""
    while events is not None and not events.empty():
        await deliver(*events.get_nowait())


async def set_name(g: str, name: str | None) -> None:
    """Гость ввёл/сменил имя (в личном или общем чате): тема переименовывается, событие в теме."""
    row = guest_row(g)
    if not row or not name or name == row[2]:
        return
    q("UPDATE guests SET name=? WHERE sid=?", (name, g))
    say(g, f"✏️ Имя: {'«' + row[2] + '»' if row[2] else 'не было'} → «{name}»")
    if row[1]:
        try:
            await tg("editForumTopic", chat_id=TG_CHAT_ID, message_thread_id=row[1], name=topic_title(row[0], name))
        except Exception as e:
            print("rename error:", e)


def guest_of_mac(mac: str) -> str | None:
    row = q("SELECT sid FROM guest_keys WHERE key=?", ("mac:" + mac,)) or q("SELECT sid FROM devices WHERE mac=?", (mac,))
    return canon(row[0][0]) if row else None


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
            q("INSERT OR REPLACE INTO bans VALUES(?,?)", (canon(row[0][0]), time.time()))
            n = q("SELECT COUNT(*) FROM room WHERE sid=? AND deleted=0", (row[0][0],))[0][0]
            q("UPDATE room SET deleted=1 WHERE sid=?", (row[0][0],))
            reply = f"🚫 {row[0][1]} забанен, удалено сообщений: {n}"
        elif cmd == "unban":
            q("DELETE FROM bans WHERE sid=?", (canon(row[0][0]),))
            reply = f"✅ {row[0][1]} разбанен"
    try:
        await tg("sendMessage", chat_id=TG_CHAT_ID, text=reply, reply_to_message_id=m["message_id"],
                 **({"message_thread_id": m["message_thread_id"]} if m.get("is_topic_message") else {}))
    except Exception as e:
        print("moderate reply error:", e)


# ---------- журнал посещений ----------
visit_notify = Limiter(20, 60)  # всего уведомлений о заходах в минуту


async def notify_visit(vid: int) -> None:
    """Заход — в тему этого человека (тема создаётся сразу). Боты и превью ссылок (страница без JS) — только в журнал."""
    try:
        for wait in (6, 20):  # ждём уточнения от страницы (модель, экран)
            await asyncio.sleep(wait)
            r = visits.rows(q, "v.id=? AND v.sent=0", (vid,), 1)
            if not r or r[0]["kind"] == "бот":
                return
            if r[0]["screen"] or r[0]["mac"]:
                break
        else:
            return
        r = r[0]
        if not visit_notify.ok("all"):
            return
        q("UPDATE visits SET sent=1 WHERE id=?", (vid,))
        g = canon(q("SELECT sid FROM visits WHERE id=?", (vid,))[0][0])
        ss = sids_of(g)
        before = q(f"SELECT COUNT(*) FROM visits WHERE sid IN ({','.join('?' * len(ss))}) AND id<?", (*ss, vid))[0][0]
        gr = guest_row(g)
        r["num"], r["name"] = (gr[0], gr[2]) if gr else (None, None)
        head = "🆕 Новый посетитель" if not before else f"🔁 Снова зашёл (визит {before + 1})"
        say(g, head + "\n" + visits.line(r), loud=True)
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


async def reply(m: dict, text: str) -> None:
    thread = {"message_thread_id": m["message_thread_id"]} if m.get("is_topic_message") else {}
    for i in range(0, len(text), 3800):
        try:
            await tg("sendMessage", chat_id=TG_CHAT_ID, text=text[i:i + 3800], link_preview_options={"is_disabled": True}, **thread)
        except Exception as e:
            print("reply error:", e)


def fmt_min(sec: int) -> str:
    return f"{sec // 3600} ч {sec % 3600 // 60} мин" if sec >= 3600 else f"{sec // 60} мин"


# ---------- реклама на странице ----------
AD_TEXT_MAX, AD_CONTACT_MAX = 200, 40
AD_HELP = ("Реклама на странице:\n/ad — список и статистика\n"
           "/ad add 30 Текст | +992 900 00 00 00 — разместить на 30 дней (контакт после | необязателен)\n"
           "/ad del 3 — снять объявление #3\n"
           "Пока своих объявлений нет, на странице видно «Место для вашей рекламы» с примерами.")


def ad_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((ts or time.time()) + 18000))  # день по Душанбе


def ad_reach(days: int = 7) -> int:
    """Сколько разных устройств открывали страницу — эту цифру показываем рекламодателю."""
    return q("SELECT COUNT(DISTINCT COALESCE(NULLIF(dev,''), sid)) FROM visits WHERE last>?", (time.time() - days * 86400,))[0][0]


def ads_active(now: float | None = None) -> list:
    return q("SELECT id, text, contact FROM ads WHERE off=0 AND until>? ORDER BY id", (now or time.time(),))


def ad_list() -> str:
    rows = q("SELECT id, text, contact, until, off FROM ads ORDER BY id DESC LIMIT 15")
    week = ad_day(time.time() - 6 * 86400)
    out = [f"📣 Реклама · охват страницы: {ad_reach()} устройств за 7 дней"]
    for i, t, c, until, off in rows:
        shows, clicks = q("SELECT COUNT(*), COALESCE(SUM(click),0) FROM ad_stats WHERE ad_id=?", (i,))[0]
        wk = q("SELECT COUNT(*) FROM ad_stats WHERE ad_id=? AND day>=?", (i, week))[0][0]
        state = "снято" if off else ("до " + time.strftime("%d.%m", time.gmtime(until + 18000)) if until > time.time() else "истекло")
        out.append(f"#{i} · {state} · показов {shows} (за 7 дн. {wk}) · нажатий {clicks}\n{t}" + (f" | {c}" if c else ""))
    if len(out) == 1:
        out.append("Объявлений нет — на странице «Место для вашей рекламы».")
    return "\n\n".join(out) + "\n\n" + AD_HELP.split("\n", 1)[1]


async def ad_cmd(m: dict, text: str) -> None:
    parts = text.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else ""
    try:
        async with db_lock:
            if sub == "add" and len(parts) > 2:
                rest = parts[2]
                days = 30
                mm = re.match(r"^(\d{1,3})\s+(.+)$", rest, re.S)
                if mm:
                    days, rest = max(1, int(mm.group(1))), mm.group(2)
                body, _, contact = rest.partition("|")
                body, contact = clean(body, AD_TEXT_MAX), clean(contact, AD_CONTACT_MAX)
                if len(body) < 3:
                    raise access.AccessError("Нужен текст объявления")
                now = time.time()
                q("INSERT INTO ads(text, contact, created, until) VALUES(?,?,?,?)", (body, contact, now, now + days * 86400))
                out = f"✅ Объявление #{q('SELECT MAX(id) FROM ads')[0][0]} на {days} дн. — уже на странице"
            elif sub == "del" and len(parts) > 2 and parts[2].strip().lstrip("#").isdigit():
                n = int(parts[2].strip().lstrip("#"))
                if not q("SELECT 1 FROM ads WHERE id=?", (n,)):
                    raise access.AccessError("Нет объявления #" + str(n))
                q("UPDATE ads SET off=1 WHERE id=?", (n,))
                out = f"✅ Объявление #{n} снято (статистика сохранена)"
            elif sub in ("", "list", "stats"):
                out = ad_list()
            else:
                out = AD_HELP
    except access.AccessError as e:
        out = "⚠️ " + str(e)
    await reply(m, out)


async def access_cmd(m: dict, text: str) -> None:
    """Админ-команды доступа (только из группы админов)."""
    parts = text.split()
    cmd, args = parts[0].split("@")[0][1:], parts[1:]
    out = ""
    try:
        async with db_lock:
            def uid_of(login):
                row = q("SELECT id FROM users WHERE login=?", (access.norm_login(login),))
                if not row:
                    raise access.AccessError("Нет такого пользователя")
                return row[0][0]
            if cmd == "adduser" and args:
                pw = secrets.token_urlsafe(6)
                uid = access.create_user(q, args[0], pw, "admin")
                mins = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
                q("UPDATE users SET balance=balance+? WHERE id=?", (mins * 60, uid))
                out = f"✅ {args[0].lower()} · пароль: {pw} · баланс {mins} мин\nПароль покажите гостю и удалите это сообщение."
            elif cmd == "addtime" and len(args) == 2 and args[1].lstrip("-").isdigit():
                uid = uid_of(args[0])
                q("UPDATE users SET balance=MAX(0, balance+?) WHERE id=?", (int(args[1]) * 60, uid))
                access.log(q, uid, None, "admin_time", int(args[1]) * 60)
                out = f"✅ баланс {args[0]}: {fmt_min(q('SELECT balance FROM users WHERE id=?', (uid,))[0][0])}"
            elif cmd == "user" and args:
                uid = uid_of(args[0])
                login, bal, blk, created, by = q("SELECT login,balance,blocked,created,by FROM users WHERE id=?", (uid,))[0]
                devs = [r[0] for r in q("SELECT mac FROM user_devices WHERE user_id=?", (uid,))]
                s = access.active(q, uid)
                last = q("SELECT ts,mac,event,secs FROM access_log WHERE user_id=? ORDER BY ts DESC LIMIT 8", (uid,))
                out = (f"👤 {login}{' ⛔' if blk else ''} · создан {time.strftime('%d.%m %H:%M', time.gmtime(created + 18000))} ({by})\n"
                       f"баланс {fmt_min(bal)} · устройства: {', '.join(devs) or '—'}\n"
                       f"сейчас: {('онлайн ' + s[3] + ' на ' + s[0] + ', осталось ' + fmt_min(int(s[5] - time.time()))) if s else 'не в сети'}\n\n"
                       + "\n".join(f"{time.strftime('%d.%m %H:%M', time.gmtime(t + 18000))} {e} {mac or ''} {fmt_min(sec) if sec else ''}"
                                   for t, mac, e, sec in last))
            elif cmd in ("kick", "block", "unblock") and args:
                uid = uid_of(args[0])
                if cmd != "unblock":
                    access.stop(q, uid, cmd)  # Chilli отключит в конце уже выданного куска (≤ RADIUS_CHUNK)
                if cmd == "block":
                    q("UPDATE users SET blocked=1 WHERE id=?", (uid,)); q("DELETE FROM user_sids WHERE user_id=?", (uid,))
                if cmd == "unblock":
                    q("UPDATE users SET blocked=0 WHERE id=?", (uid,))
                access.log(q, uid, None, "admin_" + cmd)
                out = f"✅ {cmd} {args[0]}"
            elif cmd == "resetdev" and args:
                uid = uid_of(args[0])
                q("DELETE FROM user_devices WHERE user_id=?", (uid,))
                out = f"✅ устройства {args[0]} сброшены"
            elif cmd == "codes" and len(args) >= 1 and args[0].isdigit():
                n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
                cs = access.make_codes(q, int(args[0]), n)
                out = f"🎟 Коды на {args[0]} мин ({n} шт.):\n" + "\n".join(cs)
            elif cmd == "dash":
                if args and args[0] == "off":  # выйти из админки на всех устройствах
                    q("INSERT OR REPLACE INTO kv VALUES('adm_epoch',?)", (secrets.token_hex(4),))
                    out = "✅ Все входы в панель сброшены"
                else:
                    out = (f"📊 Панель (ссылка одноразовая, 10 мин):\n{PORTAL_URL.rstrip('/')}/admin?t={admin_token(600)}\n"
                           "Вход действует 12 ч на этом устройстве. /dash off — выйти везде.")
            elif cmd == "online":
                rows = q("SELECT u.login, s.mode, s.mac, s.until FROM sessions s JOIN users u ON u.id=s.user_id ORDER BY s.until")
                out = f"🌐 В сети: {len(rows)}\n" + "\n".join(
                    f"{l} · {md} · {mac} · ещё {fmt_min(int(u - time.time()))}" for l, md, mac, u in rows)
            else:
                out = ("Команды доступа:\n/adduser логин [минуты] — создать (пароль придумает бот)\n"
                       "/addtime логин минуты — добавить (или -минуты)\n/user логин — карточка и журнал\n"
                       "/kick логин — отключить сейчас\n/block, /unblock логин\n/resetdev логин — сбросить привязку устройств\n"
                       "/codes минуты [кол-во] — коды пополнения\n/online — кто в сети\n/dash — ссылка на панель (кто в сети, трафик, история)\n/ad — реклама на странице")
    except access.AccessError as e:
        out = "⚠️ " + str(e)
    await reply(m, out)


async def access_loop():
    """Закрывает истёкшие сессии в базе (на роутере их закрывает Session-Timeout из RADIUS)."""
    while True:
        try:
            async with db_lock:
                for uid, mac in access.expired(q):
                    s = access.active(q, uid)
                    access.stop(q, uid, "expired")
                    say(guest_of_mac(mac) or (s and s[1]), "⌛ Время интернета закончилось")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("access loop error:", e)
        await asyncio.sleep(15)


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
    ss = sids_of(sid)
    marks = ",".join("?" * len(ss))
    cur = q(f"SELECT display FROM room_names WHERE sid IN ({marks}) ORDER BY sid=? DESC LIMIT 1", (*ss, sid))
    if cur and (cur[0][0] == want or re.fullmatch(re.escape(want) + r" \d+", cur[0][0], re.I)):
        return cur[0][0]
    q(f"DELETE FROM room_names WHERE sid IN ({marks}) AND sid<>?", (*ss, sid))  # старые ники этого же человека
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


def topic_guest_num(thread_id: int) -> int | None:
    """Чья это тема. Тема, закрытая при объединении гостей, ведёт к тому же человеку."""
    row = q("SELECT num FROM guests WHERE topic_id=?", (thread_id,))
    if not row:
        k = q("SELECT sid FROM guest_keys WHERE key=?", (f"topic:{thread_id}",))
        row = q("SELECT num FROM guests WHERE sid=?", (canon(k[0][0]),)) if k else []
    return row[0][0] if row else None


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
                if text and re.match(r"^/ad(@\w+)?(\s|$)", text):
                    await ad_cmd(m, text)
                    continue
                if text and re.match(r"^/news(@\w+)?(\s|$)", text):
                    await set_news(m, text)
                    continue
                if text and re.match(r"^/(visits|stats|who)(@\w+)?(\s|$)", text):
                    await visits_cmd(m, text)
                    continue
                if text and re.match(r"^/(adduser|addtime|user|kick|block|unblock|resetdev|codes|online|dash)(@\w+)?(\s|$)", text):
                    await access_cmd(m, text)
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
                num = topic_guest_num(m["message_thread_id"])
                if num:
                    q("INSERT INTO messages(num,out,text,ts) VALUES(?,1,?,?)", (num, clean(text, 4000), time.time()))
            q("INSERT OR REPLACE INTO kv VALUES('offset',?)", (str(offset),))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # сеть/telegram — подождать и продолжить
            print("poll error:", e)
            await asyncio.sleep(5)


def cache_key() -> str:
    return hmac.new(UAM_SECRET.encode(), b"cache-export", hashlib.sha256).hexdigest()


async def take_over_cache() -> int:
    """При деплое старая версия ещё отвечает по публичному адресу — забираем у неё готовые данные.
    Внешние сервисы тогда опрашиваются только в HH:00, а не при каждом деплое.
    Возвращает число строк базы (0 — старой версии нет: сервер проснулся после сна)."""
    if not (UAM_SECRET and PORTAL_URL):
        return 0
    try:
        r = await http.get(PORTAL_URL.rstrip("/") + "/api/cache-export",
                           headers={"X-Cache-Key": cache_key()}, timeout=6)
        if r.status_code == 200:
            d = r.json()
            n = import_db(d.get("db"))
            print("cache taken over:", info.import_state(d), "sources,", n, "db rows")
            return n
        print("cache take-over: HTTP", r.status_code)
    except Exception as e:
        print("cache take-over failed:", type(e).__name__)  # обычный холодный старт
    return 0


# ---------- резервная копия базы в Telegram (см. backup.py) ----------
# По умолчанию включена только на Render: локальный запуск с тем же ботом не должен трогать боевую копию.
BACKUP_ON = os.environ.get("TG_BACKUP", "1" if os.environ.get("RENDER") else "0") == "1"
BACKUP_EVERY = 600
bak: backup.TgBackup | None = None
restore_tries = 0  # >0 — копию не удалось прочитать из-за сбоя: пока не сохраняем поверх неё


def db_empty() -> bool:
    return not q("SELECT 1 FROM kv LIMIT 1") and not q("SELECT 1 FROM guests LIMIT 1")


def backup_state() -> dict:
    d = info.export_state()
    d.pop("images", None)             # картинки тяжёлые и живут час — после сна скачаются заново
    d.get("cache", {}).pop("wiki", None)  # список фото без самих картинок не нужен
    d["db"] = export_db()
    return d


def wipe_db() -> None:
    for tbl in DB_EXPORT:
        if tbl != "kv":
            db.execute(f"DELETE FROM {tbl}")
    db.execute("DELETE FROM kv WHERE k<>'offset'")  # позицию в ленте Telegram не терять


async def restore_backup(replace: bool = False) -> bool:
    """True — вопрос закрыт (восстановили или копии нет), False — сбой, надо повторить.
    replace=True — копия читается поздно, после того как сервер уже работал: то, что набралось за это время,
    стирается и база заменяется копией целиком. Смешивать нельзя: номера гостей и аккаунтов совпадут
    и чужая переписка/баланс достанутся новому гостю."""
    try:
        d = await asyncio.wait_for(bak.load(), 30)
    except backup.NoBackup as e:
        print("backup: nothing to restore:", e)
        return True
    except Exception as e:
        print("backup: restore failed:", type(e).__name__, e)
        return False
    async with db_lock:
        if replace:
            wipe_db()
        print("backup restored:", info.import_state(d), "sources,", import_db(d.get("db")), "db rows")
    return True


async def load_state() -> None:
    """Выполняется до приёма запросов: первый гость уже видит старые темы, балансы и номера."""
    global restore_tries
    took = await take_over_cache()
    if not bak or took or not db_empty():
        return
    for wait in (2, 5, 10):
        if await restore_backup():
            return
        await asyncio.sleep(wait)
    restore_tries = 1
    spawn(warn_admin("⚠️ Сервер проснулся, но не смог прочитать резервную копию базы из Telegram. "
                     "Копия не тронута и не будет перезаписана; повторяю каждые 10 минут. "
                     "Пока не прочитается, новые балансы и темы после восстановления не сохранятся."))


async def warn_admin(text: str) -> None:
    try:
        await tg("sendMessage", chat_id=TG_CHAT_ID, text=text)
    except Exception as e:
        print("warn admin error:", e)


async def save_backup() -> None:
    try:
        if await bak.save(backup_state()):
            print("backup saved")
    except Exception as e:
        print("backup: save failed:", type(e).__name__, e)


async def backup_loop():
    global restore_tries
    await asyncio.sleep(120)
    while True:
        if restore_tries:  # копия есть, но при старте не прочиталась: пробуем ещё; поверх неё не пишем НИКОГДА
            if await restore_backup(replace=True):
                restore_tries = 0
                spawn(warn_admin("✅ Резервная копия базы прочитана, данные восстановлены."))
            else:
                restore_tries += 1
        if not restore_tries:
            await save_backup()
        await asyncio.sleep(BACKUP_EVERY)


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
    global http, bak, events
    http = httpx.AsyncClient()
    events = asyncio.Queue()
    if BACKUP_ON:
        bak = backup.TgBackup(http, TG_TOKEN, TG_CHAT_ID)
    await load_state()  # данные предыдущей версии или резервной копии — до первого запроса
    task = asyncio.create_task(poll_telegram())
    hourly = asyncio.create_task(refresh_hourly())
    acc = asyncio.create_task(access_loop())
    bk = asyncio.create_task(backup_loop()) if bak else None
    ev = asyncio.create_task(events_loop())
    yield
    ev.cancel()
    task.cancel()
    hourly.cancel()
    acc.cancel()
    if bk:
        bk.cancel()
        if not restore_tries:  # перед сном/перезапуском — свежая копия
            try:
                await asyncio.wait_for(save_backup(), 20)
            except Exception as e:
                print("backup: final save failed:", type(e).__name__)
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


def save_chilli_ctx(request: Request, mac: str) -> None:
    """Запомнить, где у этого устройства Chilli (uamip:uamport) и его challenge — только из подписанного запроса."""
    p = request.query_params
    try:
        ip = ipaddress.ip_address(p.get("uamip", ""))
        port = int(p.get("uamport", ""))
    except ValueError:
        return
    ch = p.get("challenge", "")
    if ip.is_private and 1 <= port <= 65535 and re.fullmatch(r"[0-9a-fA-F]{32}", ch):
        q("INSERT OR REPLACE INTO chilli_ctx VALUES(?,?,?,?,?)", (mac, str(ip), port, ch.lower(), time.time()))


def chilli_logon_url(mac: str, user: str, pw: str) -> str | None:
    """Ссылка входа в Chilli. Пароль кодируется challenge + uamsecret (как hotspotlogin.cgi)."""
    row = q("SELECT uamip, uamport, challenge FROM chilli_ctx WHERE mac=?", (mac,))
    if not row:
        return None
    ip, port, ch = row[0]
    newchal = hashlib.md5(bytes.fromhex(ch) + UAM_SECRET.encode()).digest() if UAM_SECRET else bytes.fromhex(ch)
    raw = pw.encode()[:16].ljust(16, b"\0")
    enc = bytes(a ^ b for a, b in zip(raw, newchal)).hex()
    from urllib.parse import urlencode
    return f"http://{ip}:{port}/logon?" + urlencode({"username": user, "password": enc, "userurl": PORTAL_URL or "/"})


def chilli_logoff_url(mac: str) -> str | None:
    row = q("SELECT uamip, uamport FROM chilli_ctx WHERE mac=?", (mac,))
    return f"http://{row[0][0]}:{row[0][1]}/logoff" if row else None


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
        async with db_lock:  # не во время создания темы этому же гостю (ensure_topic держит замок)
            row = q("SELECT sid FROM devices WHERE mac=?", (mac,))
            if row:
                dsid = row[0][0]
            else:
                dsid = sid or secrets.token_urlsafe(32)
                q("INSERT INTO devices(mac,sid,seen) VALUES(?,?,?)", (mac, dsid, time.time()))
            link(dsid, "mac:" + mac, "MAC телефона от роутера")
            if sid and sid != dsid:  # у браузера была своя cookie — это тот же человек
                link(sid, "mac:" + mac, "MAC телефона от роутера")
        sid = dsid
        q("UPDATE devices SET seen=? WHERE mac=?", (time.time(), mac))
        save_chilli_ctx(request, mac)
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
    ads = ads_active()
    sid, day = get_sid(request), ad_day()
    for a in ads:  # показ = одно устройство один раз в день
        q("INSERT OR IGNORE INTO ad_stats(ad_id, sid, day) VALUES(?,?,?)", (a[0], sid, day))
    data["ads"] = [{"id": i, "text": t, "contact": c} for i, t, c in ads]
    data["ad_reach"] = ad_reach() if not ads else 0
    return data


class AdClickIn(BaseModel):
    id: int = Field(ge=1, le=10**9)


ad_lim = Limiter(20, 60)


@app.post("/api/ad/click")
async def ad_click(body: AdClickIn, request: Request):
    require_page(request)
    sid = get_sid(request)
    if ad_lim.ok(sid) and q("SELECT 1 FROM ads WHERE id=? AND off=0 AND until>?", (body.id, time.time())):
        day = ad_day()
        q("INSERT OR IGNORE INTO ad_stats(ad_id, sid, day) VALUES(?,?,?)", (body.id, sid, day))
        q("UPDATE ad_stats SET click=1 WHERE ad_id=? AND sid=? AND day=?", (body.id, sid, day))
    return {"ok": True}


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
    "guests": 100000, "guest_keys": 1000000, "devices": 100000, "bans": 100000, "room_names": 100000, "kv": 1000,
    "room": 1000, "messages": 5000, "visits": 20000,
    # доступ в интернет: аккаунты и балансы обязаны пережить деплой
    "users": 1000000, "user_sids": 1000000, "user_devices": 1000000, "sessions": 100000,
    "free_use": 200000, "reg_marks": 200000, "dev_users": 1000000, "codes": 1000000, "access_log": 50000, "chilli_ctx": 100000, "net_acct": 50000,
    "ads": 1000, "ad_stats": 200000,
}


@app.get("/api/cache-export")
async def cache_export(request: Request):
    key = request.headers.get("x-cache-key", "")
    if not (UAM_SECRET and hmac.compare_digest(key, cache_key())):
        raise HTTPException(404)
    data = info.export_state()
    data["db"] = export_db()
    return data


def export_db() -> dict:
    tables = {}
    for tbl, lim in DB_EXPORT.items():
        cur = db.execute(f"SELECT * FROM (SELECT rowid AS _r, * FROM {tbl} ORDER BY rowid DESC LIMIT ?) ORDER BY _r", (lim,))
        cols = [c[0] for c in cur.description][1:]
        tables[tbl] = {"cols": cols, "rows": [list(r[1:]) for r in cur.fetchall()]}
    return tables


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
    if name not in {"chat.js", "chat.css", "info.js", "game.js", "access.js", "admin.js", "admin.css"}:
        raise HTTPException(404)
    return FileResponse(BASE / "static" / name)


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT * 2)
    name: str | None = Field(default=None, max_length=MAX_NAME * 2)


@app.get("/api/me")
async def me(request: Request):
    sid = canon(require_sid(request))
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

    g = canon(sid)
    await ensure_topic(g)
    await set_name(g, name)
    try:
        num = await topic_send(g, "✉️ " + text)
    except RuntimeError:
        raise HTTPException(502, "Не удалось отправить, попробуйте ещё раз")
    q("INSERT INTO messages(num,out,text,ts) VALUES(?,0,?,?)", (num, text, time.time()))
    return {"ok": True, "num": num}


@app.get("/api/messages")
async def messages(request: Request, after: int = 0):
    sid = require_sid(request)
    if len(online) < 10000:
        online[sid] = time.time()
    row = q("SELECT num FROM guests WHERE sid=?", (canon(sid),))
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
    raw = require_sid(request)
    sid = canon(raw)
    if q("SELECT 1 FROM bans WHERE sid=?", (sid,)):
        raise HTTPException(403, "Вам запрещено писать в общий чат")
    if ROOM_WIFI_ONLY and not from_wifi(request, raw):
        raise HTTPException(403, "Писать в общий чат можно только подключившись к Free Wifi")
    text = clean(body.text, ROOM_MAX)
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if LINK_RE.search(text):
        raise HTTPException(400, "Ссылки в общем чате запрещены")
    if not (room_burst.ok(sid) and room_guest.ok(sid) and room_ip.ok(client_ip(request)) and global_lim.ok("all")):
        raise HTTPException(429, "Не так быстро 🙂")
    num = guest_num(sid)
    await set_name(sid, clean(body.name or "", MAX_NAME) or None)
    name = display_name(sid, num, clean(body.name or "", MAX_NAME) or None)
    text = censor(text)
    cur = db.execute("INSERT INTO room(sid,num,name,text,ts) VALUES(?,?,?,?,?)", (sid, num, name, text, time.time()))
    mid = cur.lastrowid
    q("DELETE FROM room WHERE id <= ?", (mid - ROOM_KEEP,))
    try:
        await tg_service("room_topic", room_topic, text=f"#{mid} {name} (Анон{num}): {text}",
                         link_preview_options={"is_disabled": True})
    except Exception as e:
        print("room copy error:", e)
    say(sid, f"💬 Написал в общий чат (#{mid}): {text}")
    return {"ok": True, "id": mid, "name": name}


# ---------- доступ в интернет ----------
DEVICE_FRESH = 12 * 3600  # MAC из подписанного редиректа роутера считается актуальным 12 часов


def sid_mac(sid: str) -> str | None:
    """MAC этого устройства — только из подписанного редиректа роутера, не со слов браузера."""
    row = q("SELECT mac FROM devices WHERE sid=? AND seen>? ORDER BY seen DESC LIMIT 1", (sid, time.time() - DEVICE_FRESH))
    return row[0][0] if row else None


def norm_fp(fp: str | None) -> str | None:
    fp = (fp or "").lower()
    return fp if access.FP_RE.match(fp) else None


auth_sid = Limiter(8, 900)     # попыток входа/регистрации с одной сессии за 15 мин
auth_login = Limiter(5, 900)   # попыток на один логин за 15 мин (перебор пароля)
auth_all = Limiter(300, 900)   # всего
redeem_lim = Limiter(5, 900)   # ввод кодов
act_lim = Limiter(20, 60)      # старт/стоп


class AuthIn(BaseModel):
    login: str = Field(max_length=40)
    password: str = Field(max_length=128)
    fp: str | None = Field(default=None, max_length=64)


class StartIn(BaseModel):
    mode: str = Field(max_length=8)
    fp: str | None = Field(default=None, max_length=64)


class CodeIn(BaseModel):
    code: str = Field(max_length=20)


class PwIn(BaseModel):
    old: str = Field(max_length=128)
    new: str = Field(max_length=128)


def acc_err(e: access.AccessError):
    return HTTPException(400, str(e))


def link_fp(sid: str, fp: str | None, ua: str) -> None:
    """Отпечаток браузера — только подсказка, и только когда MAC неизвестен и браузер в точности тот же."""
    if not fp:
        return
    me, key = canon(sid), "fp:" + fp
    row = q("SELECT sid FROM guest_keys WHERE key=?", (key,))
    if not row:
        q("INSERT INTO guest_keys VALUES(?,?,?)", (key, me, time.time()))
        return
    other = canon(row[0][0])
    if other == me or sid_mac(sid) or q("SELECT 1 FROM guest_keys WHERE sid=? AND key LIKE 'mac:%'", (me,)):
        return
    ss = sids_of(other)
    last = q(f"SELECT ua FROM visits WHERE sid IN ({','.join('?' * len(ss))}) ORDER BY id DESC LIMIT 1", ss)
    if last and last[0][0] == ua[:400]:
        hint(sid, key, "Отпечаток браузера (телефон той же модели, тот же браузер)")


@app.get("/api/access")
async def access_state(request: Request, fp: str = ""):
    sid = require_sid(request)
    async with db_lock:
        link_fp(sid, norm_fp(fp), request.headers.get("user-agent", ""))
        return access.state(q, sid, sid_mac(sid), norm_fp(fp))


ACT_NAMES = {"login": "Вход в аккаунт", "register": "Регистрация", "logout": "Выход из аккаунта",
             "start": "Включение интернета", "relogon": "Повторный вход в роутер", "stop": "Выключение интернета",
             "redeem": "Ввод кода", "password": "Смена пароля"}


def mins_txt(secs: int) -> str:
    m = (int(secs) + 30) // 60  # до ближайшей минуты: 299 с — это «5 мин»
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{m} мин"


@app.post("/api/access/{action}")
async def access_action(action: str, request: Request):
    sid = require_sid(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    mac = sid_mac(sid)
    nav = None  # куда отправить браузер: вход/выход в Chilli
    what = ACT_NAMES.get(action, action)
    if action in ("login", "register") and isinstance(body, dict):
        what += f" «{clean(str(body.get('login', '')), 40)}»"  # логин видно, пароль — никогда
    ev = None
    try:
        if action in ("login", "register"):
            b = AuthIn(**body)
            if not (auth_sid.ok(sid) and auth_login.ok(b.login.strip().lower()) and auth_all.ok("all")):
                raise HTTPException(429, "Слишком много попыток. Подождите 15 минут")
            async with db_lock:
                if action == "login":
                    uid = access.login_user(q, sid, b.login, b.password)
                else:
                    uid = access.register(q, sid, mac, norm_fp(b.fp), b.login, b.password)
                login = q("SELECT login FROM users WHERE id=?", (uid,))[0][0]
                hint(sid, f"user:{uid}", f"Аккаунт «{login}» (логином могут делиться)")
            ev = (f"🔑 Вошёл в аккаунт «{login}»" if action == "login" else f"🆕 Зарегистрировал аккаунт «{login}»")
        elif action == "logout":
            async with db_lock:
                access.logout(q, sid)
            ev = "🚪 Вышел из аккаунта"
        else:
            async with db_lock:
                # без пароля: аккаунт устройства создаётся сам (для старта и кодов)
                uid = access.user_of(q, sid) or (access.quick_user(q, sid, mac, norm_fp(body.get("fp")))
                                                 if action in ("start", "redeem") else None)
            if not uid:
                raise HTTPException(401, "Подключитесь к сети «Assalom Wi-Fi»")
            if action in ("start", "relogon"):
                if not act_lim.ok(sid):
                    raise HTTPException(429, "Слишком часто")
                async with db_lock:
                    if action == "start":
                        b = StartIn(**body)
                        access.start(q, uid, sid, mac, norm_fp(b.fp), b.mode)
                    tok = access.relogon(q, uid, mac)
                    nav = chilli_logon_url(mac, *tok)
                    if not nav:
                        if action == "start":
                            access.stop(q, uid, "no_chilli")
                        raise access.AccessError("Переподключитесь к «Assalom Wi-Fi» и откройте страницу заново")
                ev = action  # текст — ниже, когда известно, сколько времени выдано
            elif action == "stop":
                async with db_lock:
                    s = access.active(q, uid)
                    if s and s[0] != mac:
                        raise access.AccessError("Остановить можно с того устройства, где включено")
                    access.stop(q, uid, "user")
                    nav = chilli_logoff_url(mac) if mac else None
                ev = "⏹ Выключил интернет"
            elif action == "redeem":
                b = CodeIn(**body)
                if not redeem_lim.ok(sid):
                    raise HTTPException(429, "Слишком много попыток. Подождите 15 минут")
                async with db_lock:
                    n = access.redeem(q, uid, b.code)
                ev = f"🎟 Ввёл код: +{n} мин"
            elif action == "password":
                b = PwIn(**body)
                if not auth_sid.ok(sid):
                    raise HTTPException(429, "Слишком много попыток")
                async with db_lock:
                    access.change_pw(q, uid, b.old, b.new)
                ev = "🔒 Сменил пароль"
            else:
                raise HTTPException(404)
    except access.AccessError as e:
        say(sid, f"⚠️ {what}: {e}")
        raise acc_err(e)
    except HTTPException as e:
        if e.status_code == 429:
            say(sid, f"⚠️ {what}: {e.detail}")
        raise
    except (TypeError, ValueError):
        raise HTTPException(400, "bad request")
    async with db_lock:
        st = access.state(q, sid, mac, norm_fp(body.get("fp") if isinstance(body, dict) else None))
    if ev in ("start", "relogon"):
        a = st.get("active") or {}
        kind = "бесплатное время" if a.get("mode") == "free" else "по балансу"
        ev = (f"🌐 Нажал «Включить интернет» ({kind}, {mins_txt(a.get('left', 0))}) — жду, пустит ли роутер"
              if ev == "start" else "🔄 Повторный вход в роутер")
    if ev:
        say(sid, ev, loud=ev.startswith("🌐"))
    if nav:
        st["nav"] = nav
    return st


# ---------- RADIUS-посредник на ПК спрашивает решение здесь ----------
IDLE_SEC = int(os.environ.get("IDLE_SEC", "900"))  # Chilli отключит гостя после стольких секунд без трафика
RADIUS_CHUNK = int(os.environ.get("RADIUS_CHUNK", "3600"))  # максимум за один вход; kick/block срабатывают не позже
SPEED = {"free": int(os.environ.get("FREE_KBPS", "2048")), "paid": int(os.environ.get("PAID_KBPS", "5120"))}


def radius_key() -> str:
    return hmac.new(UAM_SECRET.encode(), b"radius", hashlib.sha256).hexdigest()


class RadiusIn(BaseModel):
    user: str = Field(max_length=64)
    password: str = Field(max_length=64)
    mac: str = Field(max_length=17)


@app.post("/api/radius")
async def radius(body: RadiusIn, request: Request):
    if not (UAM_SECRET and hmac.compare_digest(request.headers.get("x-radius-key", ""), radius_key())):
        raise HTTPException(404)
    mac = body.mac.upper().replace(":", "-")
    if not MAC_RE.match(mac):
        return {"accept": False}
    async with db_lock:
        r = access.radius_auth(q, body.user, body.password, mac, RADIUS_CHUNK)
    g = guest_of_mac(mac)
    if not r:
        say(g, "⛔ Роутер спросил разрешение — отказано (время не включено или вход с другого устройства)")
        return {"accept": False}
    secs, mode = r
    kbps = SPEED.get(mode, SPEED["free"])
    say(g, f"✅ Роутер открыл интернет на {mins_txt(secs)}, скорость до {kbps // 1024 if kbps >= 1024 else kbps} "
           f"{'Мбит/с' if kbps >= 1024 else 'кбит/с'}")
    return {"accept": True, "timeout": secs, "idle": IDLE_SEC, "kbps": kbps}


class AcctIn(BaseModel):
    status: str = Field(pattern=r"^(start|interim|stop)$")
    id: str = Field(min_length=1, max_length=64)
    user: str = Field(default="", max_length=64)
    mac: str = Field(max_length=17)
    ip: str = Field(default="", max_length=45)
    secs: int = Field(ge=0, le=10 ** 8)
    up: int = Field(ge=0, le=10 ** 15)
    down: int = Field(ge=0, le=10 ** 15)
    cause: str = Field(default="", max_length=16)


@app.post("/api/radius/acct")
async def radius_acct(body: AcctIn, request: Request):
    if not (UAM_SECRET and hmac.compare_digest(request.headers.get("x-radius-key", ""), radius_key())):
        raise HTTPException(404)
    mac = body.mac.upper().replace(":", "-")
    if not MAC_RE.match(mac):
        return {"ok": False}
    async with db_lock:
        access.acct_update(q, body.status, body.id, body.user, mac, body.ip, body.secs, body.up, body.down,
                           body.cause, IDLE_SEC)
    if body.status == "start":
        say(guest_of_mac(mac), f"📶 Сеанс в роутере начался" + (f", IP {body.ip}" if body.ip else ""))
    elif body.status == "stop":
        secs = max(0, body.secs - IDLE_SEC) if body.cause == "idle" else body.secs
        why = {"idle": "не было трафика", "timeout": "кончилось выданное время", "user": "гость отключился",
               "logoff": "выход со страницы", "admin": "отключил админ"}.get(body.cause, body.cause or "—")
        say(guest_of_mac(mac), f"📴 Сеанс в роутере закончен: {mins_txt(secs)}, ↓ {body.down / 1e6:.1f} МБ, "
                               f"↑ {body.up / 1e6:.1f} МБ. Причина: {why}")
    return {"ok": True}


# ---------- панель администратора (вход по одноразовой ссылке из Telegram, /dash) ----------
ADM_COOKIE, ADM_TTL = "adm", 12 * 3600
adm_used: dict[str, float] = {}


def admin_token(ttl: int) -> str:
    exp = str(int(time.time()) + ttl)
    epoch = (q("SELECT v FROM kv WHERE k='adm_epoch'") or [[""]])[0][0]
    return exp + "." + hmac.new(UAM_SECRET.encode(), f"admin:{epoch}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]


def admin_valid(tok: str | None) -> bool:
    if not (UAM_SECRET and tok and re.fullmatch(r"\d{10}\.[0-9a-f]{40}", tok)):
        return False
    exp, sig = tok.split(".")
    epoch = (q("SELECT v FROM kv WHERE k='adm_epoch'") or [[""]])[0][0]
    good = hmac.new(UAM_SECRET.encode(), f"admin:{epoch}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return int(exp) > time.time() and hmac.compare_digest(sig, good)


ADMIN_LOGIN_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Вход в панель</title><link rel="stylesheet" href="/static/admin.css"></head>
<body><h1>Assalom Wi-Fi <span class="muted">· панель</span></h1>
<form method="post" action="/admin" class="login"><input type="hidden" name="t" value="{t}">
<button type="submit">Войти в панель</button></form>
<p class="note">Ссылка одноразовая: после входа этот браузер помнит вас 12 часов.</p></body></html>"""


@app.post("/admin")
async def admin_login(request: Request):
    # вход только по нажатию кнопки: превью ссылок в Telegram и сканеры делают GET и ссылку не сжигают
    from urllib.parse import parse_qs
    t = (parse_qs((await request.body()).decode("ascii", "ignore")).get("t") or [""])[0]
    if not auth_sid.ok("adm:" + client_ip(request)) or not admin_valid(t) or t in adm_used:
        return Response("Ссылка недействительна или уже использована — возьмите новую: /dash в Telegram",
                        status_code=403, media_type="text/plain; charset=utf-8")
    now = time.time()
    for k in [k for k, v in adm_used.items() if v < now]:
        adm_used.pop(k, None)
    adm_used[t] = now + 600
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(ADM_COOKIE, admin_token(ADM_TTL), max_age=ADM_TTL, httponly=True, secure=True,
                    samesite="lax", path="/")  # lax: панель открывается и по ссылке из Telegram; API защищён заголовком
    return resp


@app.get("/admin")
async def admin_page(request: Request, t: str = ""):
    if t and admin_valid(t) and t not in adm_used:
        return Response(ADMIN_LOGIN_HTML.replace("{t}", t), media_type="text/html; charset=utf-8")
    if not admin_valid(request.cookies.get(ADM_COOKIE)):
        if t:
            return Response("Ссылка недействительна или уже использована — возьмите новую: /dash в Telegram",
                            status_code=403, media_type="text/plain; charset=utf-8")
        raise HTTPException(404)
    return FileResponse(BASE / "static" / "admin.html")


@app.get("/api/admin")
async def admin_api(request: Request):
    if not admin_valid(request.cookies.get(ADM_COOKIE)) or request.headers.get("x-requested-with") != "chat":
        raise HTTPException(404)
    async with db_lock:
        d = access.dashboard(q)
        macs = {o["mac"] for o in d["online"]} | {h["mac"] for h in d["history"]}
        dev = {}
        for mac in macs:  # чем является устройство — из журнала посещений портала
            r = q("SELECT model, os, browser, kind FROM visits WHERE mac=? ORDER BY last DESC LIMIT 1", (mac,))
            if r:
                model, os_, br, kind = r[0]
                dev[mac] = " · ".join(x for x in (model or kind, os_) if x and x != "?")
    for row in d["online"] + d["history"]:
        row["device"] = dev.get(row["mac"], "")
    return d


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return Response(status_code=404)
