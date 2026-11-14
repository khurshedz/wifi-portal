#!/usr/bin/env python3
"""RADIUS-посредник для Chilli на Keenetic: роутер спрашивает «пускать ли гостя» — мы спрашиваем портал.

Решение принимает портал (Render): /api/radius. Здесь только протокол RADIUS (RFC 2865/2866).
Только стандартная библиотека: так же запускается на ПК и на VPS.

Настройки (переменные окружения или файл, путь в ENV_FILE, формат KEY=VALUE):
  PORTAL_URL, UAM_SECRET  — те же, что на Render (ключ к /api/radius выводится из UAM_SECRET);
  RADIUS_SECRET           — общий ключ с роутером; если не задан, подбирается из стандартных;
  RADIUS_CLIENTS          — кому отвечать, по умолчанию 192.168.4.1;
  RADIUS_BIND             — адрес, по умолчанию 0.0.0.0.
Секреты никогда не пишутся в лог.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import selectors
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

ENV_FILE = os.environ.get("ENV_FILE", str(Path(__file__).resolve().parent.parent / "router_backup" / "render_env.txt"))
if Path(ENV_FILE).exists():
    for line in Path(ENV_FILE).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

PORTAL = os.environ.get("PORTAL_URL", "").rstrip("/")
UAM_SECRET = os.environ.get("UAM_SECRET", "")
CLIENTS = {x.strip() for x in os.environ.get("RADIUS_CLIENTS", "192.168.4.1").split(",") if x.strip()}
BIND = os.environ.get("RADIUS_BIND", "0.0.0.0")
CANDIDATES = [os.environ["RADIUS_SECRET"]] if os.environ.get("RADIUS_SECRET") else ["", "testing123", "secret", "keenetic", "radius", "chilli"]
TOKEN_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
KEY = hmac.new(UAM_SECRET.encode(), b"radius", hashlib.sha256).hexdigest()

ACCESS_REQUEST, ACCESS_ACCEPT, ACCESS_REJECT, ACCT_REQUEST, ACCT_RESPONSE = 1, 2, 3, 4, 5
USER_NAME, USER_PASSWORD, SESSION_TIMEOUT, IDLE_TIMEOUT, CALLING_STATION, VSA, MSG_AUTH = 1, 2, 27, 28, 31, 26, 80
WISPR = 14122  # WISPr-Bandwidth-Max-Up = 7, Max-Down = 8 (бит/с) — Chilli ими режет скорость

secret: bytes | None = os.environ["RADIUS_SECRET"].encode() if os.environ.get("RADIUS_SECRET") else None
recent: dict[tuple, tuple[float, bytes]] = {}  # повторы того же запроса — тот же ответ


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def attrs_parse(data: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i + 2 <= len(data):
        t, ln = data[i], data[i + 1]
        if ln < 2 or i + ln > len(data):
            break
        out.append((t, data[i + 2:i + ln])); i += ln
    return out


def attr(t: int, v: bytes) -> bytes:
    return bytes([t, len(v) + 2]) + v


def vsa(vendor: int, vt: int, v: bytes) -> bytes:
    return attr(VSA, struct.pack("!I", vendor) + bytes([vt, len(v) + 2]) + v)


def decode_pw(enc: bytes, sec: bytes, ra: bytes) -> bytes:
    out, prev = b"", ra
    for i in range(0, len(enc), 16):
        b = hashlib.md5(sec + prev).digest()
        c = enc[i:i + 16]
        out += bytes(x ^ y for x, y in zip(c, b)); prev = c
    return out.rstrip(b"\0")


def find_secret(enc: bytes, ra: bytes) -> tuple[bytes, bytes] | None:
    """Без заданного ключа: подходит тот, при котором пароль выглядит как наш токен (16 символов base64url)."""
    for cand in CANDIDATES:
        p = decode_pw(enc, cand.encode(), ra)
        if len(p) == 16 and set(p) <= TOKEN_CHARS:
            return cand.encode(), p
    return None


def ask_portal(user: str, pw: str, mac: str) -> dict:
    req = urllib.request.Request(PORTAL + "/api/radius", method="POST",
                                 data=json.dumps({"user": user, "password": pw, "mac": mac}).encode(),
                                 headers={"Content-Type": "application/json", "X-Radius-Key": KEY, "User-Agent": "assalom-radius"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read())


def reply(code: int, pid: int, ra: bytes, attrs: bytes, sec: bytes, with_ma: bool) -> bytes:
    if with_ma:  # Message-Authenticator: HMAC-MD5 по пакету с нулевым полем и аутентификатором запроса
        attrs += attr(MSG_AUTH, b"\0" * 16)
        pkt = struct.pack("!BBH", code, pid, 20 + len(attrs)) + ra + attrs
        ma = hmac.new(sec, pkt, hashlib.md5).digest()
        attrs = attrs[:-16] + ma
    head = struct.pack("!BBH", code, pid, 20 + len(attrs))
    auth = hashlib.md5(head + ra + attrs + sec).digest()
    return head + auth + attrs


def handle(data: bytes, src: str) -> bytes | None:
    global secret
    if len(data) < 20:
        return None
    code, pid, ln = struct.unpack("!BBH", data[:4])
    ra, a = data[4:20], attrs_parse(data[20:ln])
    get = lambda t: next((v for k, v in a if k == t), None)
    if code == ACCT_REQUEST:  # учёт не ведём (время считает портал), но отвечаем, иначе Chilli будет повторять
        sec = secret if secret is not None else CANDIDATES[0].encode()
        return reply(ACCT_RESPONSE, pid, ra, b"", sec, False)
    if code != ACCESS_REQUEST:
        return None
    user = (get(USER_NAME) or b"").decode("utf-8", "replace")
    mac = (get(CALLING_STATION) or b"").decode("ascii", "replace").upper().replace(":", "-")
    enc = get(USER_PASSWORD) or b""
    if secret is None:
        found = find_secret(enc, ra)
        if not found:
            say("не удалось подобрать общий ключ RADIUS — задайте RADIUS_SECRET (как на роутере)")
            return None
        secret = found[0]
        say("общий ключ RADIUS определён (вариант", CANDIDATES.index(secret.decode()) + 1, "из", len(CANDIDATES), ")")
    pw = decode_pw(enc, secret, ra).decode("utf-8", "replace")
    try:
        d = ask_portal(user, pw, mac)
    except Exception as e:
        say("портал не ответил:", type(e).__name__)
        d = {"accept": False}
    with_ma = get(MSG_AUTH) is not None
    if d.get("accept"):
        bps = int(d.get("kbps", 2048)) * 1024
        at = (attr(SESSION_TIMEOUT, struct.pack("!I", int(d["timeout"]))) + attr(IDLE_TIMEOUT, struct.pack("!I", int(d.get("idle", 900))))
              + vsa(WISPR, 8, struct.pack("!I", bps)) + vsa(WISPR, 7, struct.pack("!I", bps // 2)))
        say(f"ACCEPT {mac} на {int(d['timeout']) // 60} мин, {d.get('kbps')} кбит/с")
        return reply(ACCESS_ACCEPT, pid, ra, at, secret, with_ma)
    say(f"REJECT {mac}")
    return reply(ACCESS_REJECT, pid, ra, b"", secret, with_ma)


def main():
    if not (PORTAL and UAM_SECRET):
        sys.exit("нет PORTAL_URL / UAM_SECRET (ENV_FILE)")
    sel = selectors.DefaultSelector()
    for port in (1812, 1813):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((BIND, port)); s.setblocking(False)
        sel.register(s, selectors.EVENT_READ)
    say(f"RADIUS слушает {BIND}:1812/1813, клиенты: {', '.join(sorted(CLIENTS))}, портал: {PORTAL}")
    while True:
        for key, _ in sel.select():
            s = key.fileobj
            try:
                data, (ip, port) = s.recvfrom(4096)
            except OSError:
                continue
            if ip not in CLIENTS:
                continue  # чужим не отвечаем вообще
            now = time.time()
            k = (ip, port, data[1], data[4:20])
            for old in [x for x, (t, _) in recent.items() if now - t > 10]:
                recent.pop(old, None)
            if k in recent:
                out = recent[k][1]
            else:
                try:
                    out = handle(data, ip)
                except Exception as e:
                    say("ошибка обработки:", type(e).__name__, e); out = None
                if out:
                    recent[k] = (now, out)
            if out:
                s.sendto(out, (ip, port))


if __name__ == "__main__":
    main()
