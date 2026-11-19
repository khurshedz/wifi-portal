"""Один посетитель = одна тема: заходы, имя, интернет, пароли и роутер — в его теме.
Запуск: TG_BOT_TOKEN=1:x TG_CHAT_ID=-1 python -m pytest test_person.py -q (из папки chat). В сеть не ходит."""
import asyncio
import hashlib
import os
import tempfile

os.environ.setdefault("TG_BOT_TOKEN", "1:x")
os.environ.setdefault("TG_CHAT_ID", "-1")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "t.db"))
os.environ["TG_BACKUP"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from test_backup import FakeTg  # noqa: E402

PORTAL, SECRET = "https://portal.example/", "s3cret"
M1, M2 = "AA-BB-CC-00-00-01", "AA-BB-CC-00-00-02"
REDMI = ("Mozilla/5.0 (Linux; Android 11; Redmi Note 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/120.0.0.0 Mobile Safari/537.36")
SAMSUNG = ("Mozilla/5.0 (Linux; Android 13; SM-A325F) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/120.0.0.0 Mobile Safari/537.36")
H = {"X-Requested-With": "chat"}
FP = "f" * 64


def signed(mac: str) -> str:
    base = f"res=notyet&uamip=192.168.4.1&uamport=3990&challenge={'ab' * 16}&mac={mac}"
    md = hashlib.md5((PORTAL + "?" + base + SECRET).encode()).hexdigest().upper()
    return f"/?{base}&md={md}"


@pytest.fixture
def env(monkeypatch):
    tg = FakeTg()
    app.http = tg
    app.events = asyncio.Queue()
    app.guest_lim.hits.clear()
    app.new_guests.hits.clear()
    for lim in (app.auth_sid, app.auth_login, app.auth_all, app.act_lim, app.hello_lim, app.ip_lim):
        lim.hits.clear()
    monkeypatch.setattr(app, "PORTAL_URL", PORTAL)
    monkeypatch.setattr(app, "UAM_SECRET", SECRET)
    for t in list(app.DB_EXPORT) + ["chilli_ctx"]:
        app.db.execute(f"DELETE FROM {t}")
    visits = []
    monkeypatch.setattr(app, "notify_visit", lambda vid: visits.append(vid) or asyncio.sleep(0))
    yield tg, visits


async def _nosleep(_):
    return None


def notify(visits, monkeypatch):
    """Отработать отложенные уведомления о заходах и очередь событий."""
    real = app.__dict__["notify_visit"]
    monkeypatch.undo()  # вернуть настоящий notify_visit на время вызова
    monkeypatch.setattr(app, "PORTAL_URL", PORTAL)
    monkeypatch.setattr(app, "UAM_SECRET", SECRET)
    monkeypatch.setattr(app.asyncio, "sleep", _nosleep)

    async def go():
        for vid in visits:
            await app.notify_visit(vid)
        visits.clear()
        await app.drain_events()
    asyncio.run(go())
    monkeypatch.undo()
    monkeypatch.setattr(app, "PORTAL_URL", PORTAL)
    monkeypatch.setattr(app, "UAM_SECRET", SECRET)
    monkeypatch.setattr(app, "notify_visit", real)


def drain():
    asyncio.run(app.drain_events())


def phone(ua=REDMI, mac=M1, cookie=None):
    c = TestClient(app.app, base_url="https://portal.example", headers={"User-Agent": ua})
    if cookie:
        c.cookies.set("sid", cookie)
    if mac:
        r = c.get(signed(mac), follow_redirects=False)
        assert r.status_code == 303
    c.get("/")
    c.post("/api/hello", json={"screen": "393x873", "tz": "Asia/Dushanbe"}, headers=H)
    return c


def texts(tg, thread):
    return [t for th, t in tg.sent if th == thread]


def test_new_visitor_gets_own_topic_right_away(env, monkeypatch):
    tg, visits = env
    phone()
    notify(visits, monkeypatch)
    assert len(tg.topics) == 1
    tid, title = tg.topics[0]
    assert title.startswith("Анон1") and "Redmi Note 8 Pro" in title
    assert texts(tg, tid)[0].startswith("🆕 Новый посетитель") and tg.loud[0]


def test_bot_or_preview_without_js_creates_no_topic(env, monkeypatch):
    tg, visits = env
    c = TestClient(app.app, base_url="https://portal.example", headers={"User-Agent": "TelegramBot (like TwitterBot)"})
    c.get("/")
    notify(visits, monkeypatch)
    assert tg.topics == []


def test_everything_about_one_person_goes_to_one_topic(env, monkeypatch):
    tg, visits = env
    c = phone()
    notify(visits, monkeypatch)
    tid = tg.topics[0][0]

    # имя и сообщение в личном чате
    assert c.post("/api/send", json={"text": "салом", "name": "Али"}, headers=H).status_code == 200
    # неудачный вход: логин видно, пароля нет
    r = c.post("/api/access/login", json={"login": "ali", "password": "SuperSecret9"}, headers=H)
    assert r.status_code == 400
    # включил интернет, роутер спросил разрешение
    r = c.post("/api/access/start", json={"mode": "free", "fp": FP}, headers=H)
    assert r.status_code == 200 and r.json()["nav"].startswith("http://192.168.4.1:3990/logon?")
    tok_user, tok_pw = app.q("SELECT tok_user, tok_pw FROM sessions")[0]
    key = app.radius_key()
    r = c.post("/api/radius", json={"user": tok_user, "password": tok_pw, "mac": M1}, headers={"X-Radius-Key": key})
    assert r.json()["accept"] is True
    c.post("/api/radius/acct", headers={"X-Radius-Key": key}, json={
        "status": "stop", "id": "a1", "user": tok_user, "mac": M1, "ip": "10.1.30.5",
        "secs": 300, "up": 1_000_000, "down": 25_000_000, "cause": "timeout"})
    c.post("/api/access/stop", json={}, headers=H)
    drain()

    assert len(tg.topics) == 1                       # ни одной новой темы
    log = "\n".join(texts(tg, tid))
    for want in ("✉️ салом", "✏️ Имя: не было → «Али»", "⚠️ Вход в аккаунт «ali»: Неверный логин или пароль",
                 "🌐 Нажал «Включить интернет» (бесплатное время", "✅ Роутер открыл интернет на 5 мин",
                 "📴 Сеанс в роутере закончен: 5 мин, ↓ 25.0 МБ, ↑ 1.0 МБ. Причина: кончилось выданное время"):
        assert want in log, want
    assert "SuperSecret9" not in log
    assert ("editForumTopic", tid, "Анон1 · Али") in tg.edits


def test_second_browser_with_own_old_topic_is_merged(env, monkeypatch):
    tg, visits = env
    # сначала открыл страницу напрямую (без роутера, своя cookie) и написал в чат — завелась тема
    other = TestClient(app.app, base_url="https://portal.example", headers={"User-Agent": REDMI})
    other.get("/")
    old_sid = other.cookies.get("sid")
    other.post("/api/send", json={"text": "из хрома"}, headers=H)
    drain()
    old_tid = tg.topics[0][0]
    # потом этот же телефон пришёл через роутер в другом браузере
    phone()
    notify(visits, monkeypatch)
    # и тот хром тоже через роутер (MAC тот же)
    phone(cookie=old_sid)
    notify(visits, monkeypatch)
    drain()

    open_topics = {t for t, _ in tg.topics} - {t for m, t, _ in tg.edits if m == "closeForumTopic"}
    assert len(open_topics) == 1
    keep = open_topics.pop()
    assert keep == old_tid                           # осталась старая тема, история в ней
    assert any("🔗 Это тот же человек, что Анон2" in t for t in texts(tg, keep))
    closed = [t for m, t, _ in tg.edits if m == "closeForumTopic"]
    assert len(closed) == 1 and any("➡️ Это тот же человек, что Анон1" in t for t in texts(tg, closed[0]))
    g = app.canon(old_sid)
    num = app.q("SELECT num FROM guests WHERE sid=?", (g,))[0][0]
    assert app.q("SELECT COUNT(*) FROM messages WHERE num=?", (num,))[0][0] == 1


def test_two_identical_phones_with_different_mac_stay_two_people(env, monkeypatch):
    tg, visits = env
    a = phone(mac=M1)
    b = phone(mac=M2)
    for c in (a, b):
        c.get("/api/access", params={"fp": FP}, headers=H)  # одинаковый отпечаток
    notify(visits, monkeypatch)
    assert len(tg.topics) == 2
    assert app.canon(a.cookies.get("sid")) != app.canon(b.cookies.get("sid"))


def test_fingerprint_links_only_macless_session_with_same_browser(env, monkeypatch):
    tg, visits = env
    a = phone(mac=M1)
    a.get("/api/access", params={"fp": FP}, headers=H)
    notify(visits, monkeypatch)
    # тот же телефон открыл страницу напрямую (MAC неизвестен), тот же браузер → тот же человек
    same = TestClient(app.app, base_url="https://portal.example", headers={"User-Agent": REDMI})
    same.get("/")
    same.get("/api/access", params={"fp": FP}, headers=H)
    # другой телефон с совпавшим отпечатком, но другим браузером → другой человек
    diff = TestClient(app.app, base_url="https://portal.example", headers={"User-Agent": SAMSUNG})
    diff.get("/")
    diff.get("/api/access", params={"fp": FP}, headers=H)
    ga = app.canon(a.cookies.get("sid"))
    assert app.canon(same.cookies.get("sid")) == ga
    assert app.canon(diff.cookies.get("sid")) != ga


def test_deleted_guest_topic_is_recreated_for_events(env, monkeypatch):
    tg, visits = env
    c = phone()
    notify(visits, monkeypatch)
    first = tg.topics[0][0]
    tg.dead_threads.add(first)
    c.post("/api/access/login", json={"login": "nobody", "password": "x" * 8}, headers=H)
    drain()
    assert len(tg.topics) == 2
    assert any("Вход в аккаунт «nobody»" in t for t in texts(tg, tg.topics[1][0]))
