"""Доступ в интернет по пользователю, с лимитом времени.

Идея защиты: время принадлежит ПОЛЬЗОВАТЕЛЮ (логин + пароль), а не MAC-адресу.
  * Включить интернет можно только с устройства, пришедшего через роутер (подписанный MAC).
  * У пользователя одновременно активно одно устройство: вход с другого гасит прежнее.
  * На одном MAC одновременно один пользователь.
  * Новые устройства у аккаунта ограничены (DEVICE_LIMIT), сверх — только через админа.
  * Бесплатные минуты в день считаются сразу по пользователю, MAC, отпечатку устройства
    и cookie-сессии: берётся максимум потраченного. Новый аккаунт или смена MAC
    на том же телефоне лишних минут не дают.
  * Минуты списываются вперёд (резерв) при старте, неиспользованное возвращается при остановке —
    параллельные запросы не могут потратить одно и то же время дважды.
  * Регистрация — только с устройства из Wi-Fi и не чаще раза в REG_COOLDOWN на MAC/отпечаток.
Все функции синхронные и работают с переданным q(sql, args) — вызывать под общим db_lock.
"""
import hashlib
import hmac
import os
import re
import secrets
import time

FREE_SEC = int(os.environ.get("FREE_MINUTES", "5")) * 60
DEVICE_LIMIT = int(os.environ.get("DEVICE_LIMIT", "2"))
REG_COOLDOWN = 7 * 86400
TZ_SHIFT = 5 * 3600  # Душанбе, UTC+5 — граница суток для бесплатных минут

LOGIN_RE = re.compile(r"^[a-z0-9_]{3,20}$")
FP_RE = re.compile(r"^[0-9a-f]{16,64}$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O, 1/I

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT UNIQUE, pw TEXT, created REAL, by TEXT,
  balance INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_sids(sid TEXT PRIMARY KEY, user_id INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS user_devices(user_id INTEGER, mac TEXT, ts REAL, PRIMARY KEY(user_id, mac));
CREATE TABLE IF NOT EXISTS sessions(
  user_id INTEGER PRIMARY KEY, mac TEXT UNIQUE, sid TEXT, fp TEXT, mode TEXT,
  start REAL, until REAL, reserved INTEGER, keys TEXT,
  tok_user TEXT, tok_pw TEXT, granted REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS free_use(key TEXT, day TEXT, used INTEGER, PRIMARY KEY(key, day));
CREATE TABLE IF NOT EXISTS reg_marks(key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS dev_users(key TEXT PRIMARY KEY, user_id INTEGER);
CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY, minutes INTEGER, created REAL, used_by INTEGER, used_ts REAL);
CREATE TABLE IF NOT EXISTS access_log(ts REAL, user_id INTEGER, mac TEXT, event TEXT, secs INTEGER);
"""


class AccessError(Exception):
    """Ошибка, текст которой можно показать гостю."""


# ---------- пароли ----------
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + "$" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, AttributeError):
        return False


_DUMMY = hash_pw(secrets.token_hex(8))  # чтобы «нет такого логина» отвечал так же долго, как «неверный пароль»


def today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((now or time.time()) + TZ_SHIFT))


def log(q, user_id, mac, event, secs=0):
    q("INSERT INTO access_log VALUES(?,?,?,?,?)", (time.time(), user_id, mac, event, int(secs)))


# ---------- аккаунты ----------
def norm_login(login: str) -> str:
    login = (login or "").strip().lower()
    if not LOGIN_RE.match(login):
        raise AccessError("Логин: 3–20 символов, латинские буквы, цифры и _")
    return login


def check_new_pw(pw: str) -> str:
    if not (6 <= len(pw or "") <= 64):
        raise AccessError("Пароль: от 6 до 64 символов")
    return pw


def create_user(q, login: str, pw: str, by: str) -> int:
    login, pw = norm_login(login), check_new_pw(pw)
    if q("SELECT 1 FROM users WHERE login=?", (login,)):
        raise AccessError("Такой логин уже занят")
    q("INSERT INTO users(login,pw,created,by) VALUES(?,?,?,?)", (login, hash_pw(pw), time.time(), by))
    uid = q("SELECT id FROM users WHERE login=?", (login,))[0][0]
    log(q, uid, None, "create:" + by)
    return uid


def register(q, sid: str, mac: str | None, fp: str | None, login: str, pw: str) -> int:
    """Самостоятельная регистрация гостя — только из Wi-Fi и не чаще раза в неделю на устройство."""
    if not mac:
        raise AccessError("Регистрация доступна только при подключении к «Assalom Wi-Fi»")
    now = time.time()
    keys = ["mac:" + mac] + (["fp:" + fp] if fp else [])
    for k in keys:
        row = q("SELECT ts FROM reg_marks WHERE key=?", (k,))
        if row and now - row[0][0] < REG_COOLDOWN:
            raise AccessError("С этого устройства уже регистрировались. Войдите в свой аккаунт")
    uid = create_user(q, login, pw, "self")
    for k in keys:
        q("INSERT OR REPLACE INTO reg_marks VALUES(?,?)", (k, now))
    bind_sid(q, sid, uid)
    return uid


def device_user(q, mac: str | None, fp: str | None) -> int | None:
    for k in (["mac:" + mac] if mac else []) + (["fp:" + fp] if fp else []):
        row = q("SELECT user_id FROM dev_users WHERE key=?", (k,))
        if row:
            return row[0][0]
    return None


def quick_user(q, sid: str, mac: str | None, fp: str | None) -> int:
    """Идентификация сразу при заходе, без пароля, по всем признакам: cookie, MAC (подписан роутером),
    отпечаток устройства. Совпал любой — это тот же пользователь (приоритет: cookie, MAC, отпечаток).
    Новые признаки дописываются к найденному аккаунту, так что смена одного признака его не теряет."""
    uid = user_of(q, sid) or device_user(q, mac, fp)
    if not uid and not mac:
        raise AccessError("Подключитесь к сети «Assalom Wi-Fi»")
    if not uid:
        login = "d" + secrets.token_hex(4)
        q("INSERT INTO users(login,pw,created,by) VALUES(?,?,?,?)", (login, hash_pw(secrets.token_urlsafe(16)), time.time(), "auto"))
        uid = q("SELECT id FROM users WHERE login=?", (login,))[0][0]
        log(q, uid, mac, "create:auto")
    for k in (["mac:" + mac] if mac else []) + (["fp:" + fp] if fp else []):
        q("INSERT OR IGNORE INTO dev_users VALUES(?,?)", (k, uid))  # признак уже чей-то — не перехватываем
    if not user_of(q, sid):
        bind_sid(q, sid, uid)
    return uid


def login_user(q, sid: str, login: str, pw: str) -> int:
    try:
        login = norm_login(login)
    except AccessError:
        check_pw(pw or "", _DUMMY)
        raise AccessError("Неверный логин или пароль")
    row = q("SELECT id, pw, blocked FROM users WHERE login=?", (login,))
    if not row:
        check_pw(pw or "", _DUMMY)
        raise AccessError("Неверный логин или пароль")
    uid, stored, blocked = row[0]
    if not check_pw(pw or "", stored):
        log(q, uid, None, "bad_pw")
        raise AccessError("Неверный логин или пароль")
    if blocked:
        raise AccessError("Аккаунт заблокирован. Напишите администратору в чат")
    bind_sid(q, sid, uid)
    log(q, uid, None, "login")
    return uid


def bind_sid(q, sid: str, uid: int):
    q("INSERT OR REPLACE INTO user_sids VALUES(?,?,?)", (sid, uid, time.time()))


def user_of(q, sid: str) -> int | None:
    row = q("SELECT user_id FROM user_sids WHERE sid=?", (sid,))
    return row[0][0] if row else None


def logout(q, sid: str):
    q("DELETE FROM user_sids WHERE sid=?", (sid,))


def change_pw(q, uid: int, old: str, new: str):
    row = q("SELECT pw FROM users WHERE id=?", (uid,))
    if not row or not check_pw(old or "", row[0][0]):
        raise AccessError("Старый пароль неверный")
    q("UPDATE users SET pw=? WHERE id=?", (hash_pw(check_new_pw(new)), uid))
    q("DELETE FROM user_sids WHERE user_id=?", (uid,))  # выйти на всех устройствах
    log(q, uid, None, "pw_change")


# ---------- бесплатные минуты ----------
def free_keys(uid: int, mac: str, fp: str | None, sid: str) -> list[str]:
    return [f"user:{uid}", f"mac:{mac}", f"sid:{sid}"] + ([f"fp:{fp}"] if fp else [])


def free_left(q, keys: list[str], now: float | None = None) -> int:
    day, used = today(now), 0
    for k in keys:
        row = q("SELECT used FROM free_use WHERE key=? AND day=?", (k, day))
        used = max(used, row[0][0] if row else 0)
    return max(0, FREE_SEC - used)


def _free_add(q, keys, day, secs):
    for k in keys:
        q("INSERT INTO free_use VALUES(?,?,?) ON CONFLICT(key,day) DO UPDATE SET used=MAX(0, used+excluded.used)",
          (k, day, int(secs)))


# ---------- сессии ----------
def active(q, uid: int):
    row = q("SELECT mac, sid, fp, mode, start, until, reserved, keys FROM sessions WHERE user_id=?", (uid,))
    return row[0] if row else None


def stop(q, uid: int, reason: str, now: float | None = None) -> str | None:
    """Останавливает сессию и возвращает неиспользованное. Возвращает MAC, который надо закрыть на роутере."""
    now = now or time.time()
    s = active(q, uid)
    if not s:
        return None
    mac, _sid, _fp, mode, start, until, reserved, keys = s
    # уже выданное роутеру (RADIUS Session-Timeout) не возвращается: Chilli отключит гостя только в конце куска
    granted = q("SELECT granted FROM sessions WHERE user_id=?", (uid,))[0][0] or 0
    unused = max(0, int(until - max(now, granted))) if reason != "expired" else 0
    if mode == "paid":
        q("UPDATE users SET balance=balance+? WHERE id=?", (unused, uid))
    elif unused:
        _free_add(q, keys.split("|"), today(start), -unused)
    q("DELETE FROM sessions WHERE user_id=?", (uid,))
    log(q, uid, mac, f"stop:{reason}", reserved - unused)
    return mac


def device_allowed(q, uid: int, mac: str) -> bool:
    if q("SELECT 1 FROM user_devices WHERE user_id=? AND mac=?", (uid, mac)):
        return True
    n = q("SELECT COUNT(*) FROM user_devices WHERE user_id=?", (uid,))[0][0]
    if n >= DEVICE_LIMIT:
        return False
    q("INSERT INTO user_devices VALUES(?,?,?)", (uid, mac, time.time()))
    return True


def start(q, uid: int, sid: str, mac: str | None, fp: str | None, mode: str, now: float | None = None):
    """Включает интернет. Возвращает (permit_mac, seconds, [macs_to_revoke])."""
    now = now or time.time()
    if not mac:
        raise AccessError("Включить интернет можно только из сети «Assalom Wi-Fi»")
    if mode not in ("free", "paid"):
        raise AccessError("Неизвестный режим")
    row = q("SELECT blocked, balance FROM users WHERE id=?", (uid,))
    if not row or row[0][0]:
        raise AccessError("Аккаунт заблокирован")
    if not device_allowed(q, uid, mac):
        raise AccessError(f"К аккаунту уже привязано {DEVICE_LIMIT} устройства. "
                          "Новое добавит администратор (напишите в чат)")
    revoke = []
    # одно устройство на пользователя: прежняя сессия гасится (время возвращается)
    old = active(q, uid)
    if old:
        m = stop(q, uid, "replaced", now)
        if m and m != mac:
            revoke.append(m)
    # один пользователь на MAC: чужая сессия на этом устройстве гасится
    other = q("SELECT user_id FROM sessions WHERE mac=?", (mac,))
    if other:
        stop(q, other[0][0], "other_user", now)
    keys = free_keys(uid, mac, fp, sid)
    if mode == "free":
        secs = free_left(q, keys, now)
        if secs < 60:
            raise AccessError("Бесплатные минуты на сегодня закончились. Завтра снова, или пополните время кодом")
        _free_add(q, keys, today(now), secs)
    else:
        secs = q("SELECT balance FROM users WHERE id=?", (uid,))[0][0]
        if secs < 60:
            raise AccessError("На балансе нет времени. Введите код")
        q("UPDATE users SET balance=0 WHERE id=?", (uid,))
    q("INSERT INTO sessions(user_id,mac,sid,fp,mode,start,until,reserved,keys) VALUES(?,?,?,?,?,?,?,?,?)",
      (uid, mac, sid, fp, mode, now, now + secs, secs, "|".join(keys)))
    log(q, uid, mac, "start:" + mode, secs)
    return mac, secs, revoke


# ---------- вход через Chilli (RADIUS) ----------
def new_token(q, uid: int) -> tuple[str, str]:
    """Одноразовая пара логин/пароль для Chilli. Пароль аккаунта в RADIUS не уходит."""
    user, pw = "t" + secrets.token_hex(10), secrets.token_urlsafe(12)[:16]
    q("UPDATE sessions SET tok_user=?, tok_pw=? WHERE user_id=?", (user, pw, uid))
    return user, pw


def relogon(q, uid: int, mac: str | None):
    s = active(q, uid)
    if not s or not mac or s[0] != mac:
        raise AccessError("Интернет на этом устройстве не включён")
    return new_token(q, uid)


def radius_auth(q, user: str, pw: str, mac: str, chunk: int, now: float | None = None):
    """Решение для роутера: (секунд, режим) или None. Пускаем только тот MAC, для которого выдан токен."""
    now = now or time.time()
    row = q("SELECT user_id, mac, tok_pw, until, mode FROM sessions WHERE tok_user=?", (user,))
    if not row:
        return None
    uid, smac, tpw, until, mode = row[0]
    if smac != mac or not hmac.compare_digest(tpw or "", pw or "") or until - now < 5:
        log(q, uid, mac, "radius_reject")
        return None
    if q("SELECT blocked FROM users WHERE id=?", (uid,))[0][0]:
        return None
    secs = int(min(until - now, chunk))
    q("UPDATE sessions SET granted=MAX(granted, ?) WHERE user_id=?", (now + secs, uid))
    log(q, uid, mac, "radius_ok", secs)
    return secs, mode


def expired(q, now: float | None = None) -> list[tuple[int, str]]:
    now = now or time.time()
    return [(r[0], r[1]) for r in q("SELECT user_id, mac FROM sessions WHERE until<=?", (now,))]


# ---------- коды пополнения ----------
def make_codes(q, minutes: int, count: int) -> list[str]:
    if not (1 <= minutes <= 60 * 24 * 31 and 1 <= count <= 200):
        raise AccessError("минуты 1…44640, количество 1…200")
    out = []
    while len(out) < count:
        c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
        c = f"{c[:5]}-{c[5:]}"
        if not q("SELECT 1 FROM codes WHERE code=?", (c,)):
            q("INSERT INTO codes(code,minutes,created) VALUES(?,?,?)", (c, minutes, time.time()))
            out.append(c)
    return out


def redeem(q, uid: int, code: str) -> int:
    code = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    code = f"{code[:5]}-{code[5:]}" if len(code) == 10 else ""
    row = q("SELECT minutes, used_by FROM codes WHERE code=?", (code,)) if code else []
    if not row or row[0][1] is not None:
        raise AccessError("Код не найден или уже использован")
    q("UPDATE codes SET used_by=?, used_ts=? WHERE code=? AND used_by IS NULL", (uid, time.time(), code))
    q("UPDATE users SET balance=balance+? WHERE id=?", (row[0][0] * 60, uid))
    log(q, uid, None, "redeem", row[0][0] * 60)
    return row[0][0]


# ---------- состояние для страницы ----------
def state(q, sid: str, mac: str | None, fp: str | None) -> dict:
    uid = None
    try:
        uid = quick_user(q, sid, mac, fp)  # аккаунт определяется/создаётся сразу при заходе
    except AccessError:
        pass
    d = {"logged": bool(uid), "device_ok": bool(mac), "free_total": FREE_SEC // 60}
    if not uid:
        d.update(balance=0, active=None,
                 free_left=free_left(q, [f"mac:{mac}", f"sid:{sid}"] + ([f"fp:{fp}"] if fp else [])) if mac else None)
        return d
    login, balance, blocked = q("SELECT login, balance, blocked FROM users WHERE id=?", (uid,))[0]
    s = active(q, uid)
    d.update(login=login, balance=balance, blocked=bool(blocked),
             free_left=free_left(q, free_keys(uid, mac, fp, sid)) if mac else None,
             active=None)
    if s:
        d["active"] = {"mode": s[3], "left": max(0, int(s[5] - time.time())), "here": s[0] == mac}
    return d
