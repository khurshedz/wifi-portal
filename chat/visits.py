"""Журнал посещений: кто, когда, с какого устройства/браузера. Разбор User-Agent без внешних библиотек."""
import hashlib
import re
import time

TZ_SHIFT = 5 * 3600  # Душанбе, UTC+5 без перехода на летнее время
SESSION_GAP = 30 * 60  # новая запись, если гостя не было 30 минут
KEEP = 20000

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT, dev TEXT, mac TEXT, ip TEXT, wifi INTEGER,
  ua TEXT, kind TEXT, brand TEXT, model TEXT, os TEXT, browser TEXT, lang TEXT,
  screen TEXT, tz TEXT, ref TEXT, first REAL, last REAL, hits INTEGER, sent INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS visits_sid ON visits(sid, last);
"""

# модель телефона -> марка (порядок важен)
BRANDS = [
    (r"^(SM-|SAMSUNG|Galaxy|SCH-|GT-)", "Samsung"),
    (r"(Redmi|POCO|^Mi |^MI |^M2\d{3}|^2\d{3}[0-9A-Z]{4,}|Xiaomi|^MIX|^HM )", "Xiaomi"),
    (r"^Infinix|^X6\d{2}", "Infinix"),
    (r"^TECNO|^(BG|BF|BD|CK|CG|KI|KJ|LH|LG)\d", "Tecno"),
    (r"^itel|^(A|L|P|S)\d{3}[A-Z]?$", "itel"),
    (r"HONOR|^(ANY|ALI|CRT|RMO|LGE|VNE|FNE|NTH|ELI|CMA|RBN)-", "Honor"),
    (r"HUAWEI|^(VOG|ELE|ANA|JNY|MAR|POT|STK|MED|DUB|JKM|AMN|MRD|JSN|LYA|NOH|ART)-", "Huawei"),
    (r"^vivo|^V\d{4}[A-Z]?$|^I\d{4}$", "vivo"),
    (r"^OPPO|^CPH\d|^PH[A-Z]\d", "OPPO"),
    (r"^RMX\d", "realme"),
    (r"^Pixel", "Google"),
    (r"^moto|^XT\d{4}", "Motorola"),
    (r"^Nokia|^TA-\d", "Nokia"),
    (r"ONEPLUS|^(IN|LE|KB|NE|GM|HD|BE|DE|CPH)\d{4}", "OnePlus"),
    (r"^ZTE|^Blade", "ZTE"),
    (r"^Lenovo|^TB-", "Lenovo"),
    (r"^ASUS|^ZenFone|^ROG", "Asus"),
    (r"^LM-|^LG-", "LG"),
    (r"^Nothing|^A0\d{2}$", "Nothing"),
    (r"^SONY|^XQ-|^SO-", "Sony"),
    (r"^Hisense|^HLTE", "Hisense"),
    (r"^Umidigi|^Doogee|^Blackview|^BV\d|^Oukitel|^Ulefone|^Cubot", "китайский бренд"),
]

BROWSERS = [  # (regex, название) — от частного к общему
    (r"SamsungBrowser/(\d+)", "Samsung Internet"),
    (r"MiuiBrowser/(\d+)", "Mi Browser"),
    (r"YaBrowser/(\d+)|YaSearchBrowser/(\d+)", "Яндекс Браузер"),
    (r"OPR/(\d+)|OPX/(\d+)|OPT/(\d+)|Opera.*Version/(\d+)", "Opera"),
    (r"EdgA?/(\d+)|EdgiOS/(\d+)", "Edge"),
    (r"UCBrowser/(\d+)", "UC Browser"),
    (r"HeyTapBrowser/(\d+)", "OPPO Browser"),
    (r"VivoBrowser/(\d+)", "vivo Browser"),
    (r"HuaweiBrowser/(\d+)", "Huawei Browser"),
    (r"PHX/(\d+)", "Phoenix"),
    (r"Instagram ([\d]+)", "Instagram (встроенный)"),
    (r"FBAN|FBAV/(\d+)|FB_IAB", "Facebook (встроенный)"),
    (r"Telegram", "Telegram (встроенный)"),
    (r"DuckDuckGo/(\d+)", "DuckDuckGo"),
    (r"Firefox/(\d+)|FxiOS/(\d+)", "Firefox"),
    (r"CriOS/(\d+)", "Chrome"),
    (r"; wv\).*Chrome/(\d+)", "окно входа в сеть / WebView"),
    (r"Chrome/(\d+)", "Chrome"),
    (r"Version/(\d+)[\d.]* (Mobile/\S+ )?Safari/", "Safari"),
    (r"(iPhone|iPad).*AppleWebKit(?!.*Safari)", "окно входа в сеть iOS"),
    (r"CaptiveNetworkSupport|wispr", "проверка сети (система)"),
    (r"curl/|Wget|python|Go-http|okhttp|bot|spider|crawl", "бот/скрипт"),
]


def clip(s, n):
    return (s or "").strip()[:n]


def brand_of(model: str) -> str:
    for rx, name in BRANDS:
        if re.search(rx, model, re.I if name in ("Samsung", "Xiaomi", "Huawei", "Honor", "OnePlus") else 0):
            return name
    return ""


def parse_ua(ua: str, model_hint: str = "", pv_hint: str = "") -> dict:
    ua = ua or ""
    kind, brand, model, os_ = "?", "", "", "?"
    # мессенджер imo сам открывает ссылку из чата для превью: «imoAndroid/2026.08; 12; REL; ADY-LX9; HUAWEI; ...»
    if m := re.match(r"imoAndroid/[\d.]+;\s*([\d.]+);\s*\w+;\s*([^;]+);\s*([^;]+);", ua):
        model = clip(model_hint or m[2].strip(), 60)
        return {"kind": "телефон", "brand": m[3].strip().title(), "model": model, "os": f"Android {m[1]}",
                "browser": "imo — превью ссылки в чате"}
    if re.search(r"iPhone|iPad|iPod", ua):
        brand = "Apple"
        model = "iPad" if "iPad" in ua else "iPhone"
        kind = "планшет" if model == "iPad" else "телефон"
        v = re.search(r"OS (\d+)[_.](\d+)", ua)
        os_ = f"iOS {v[1]}.{v[2]}" if v else "iOS"
    elif m := re.search(r"Android\s*([\d.]+)?", ua):
        os_ = f"Android {m[1].rstrip('.')}" if m[1] else "Android"
        mm = re.search(r"Android[^;)]*;\s*(?:[a-z]{2}[-_][a-zA-Z]{2};\s*)?([^;)]+?)(?:\s+Build/[^;)]*)?\s*[;)]", ua)
        model = mm[1].strip() if mm else ""
        if model in ("K", "wv", "U", "Linux", "Mobile") or len(model) < 2:
            model = ""
        kind = "телефон" if "Mobile" in ua else "планшет"
    elif m := re.search(r"Windows NT ([\d.]+)", ua):
        os_ = {"10.0": "Windows 10/11", "6.3": "Windows 8.1", "6.2": "Windows 8", "6.1": "Windows 7"}.get(m[1], f"Windows NT {m[1]}")
        kind = "компьютер"
    elif "Macintosh" in ua:
        os_, brand, kind = "macOS", "Apple", "компьютер"
    elif "CrOS" in ua:
        os_, kind = "ChromeOS", "компьютер"
    elif "Linux" in ua:
        os_, kind = "Linux", "компьютер"
    if model_hint and not model:
        model = model_hint
    if pv_hint and os_.startswith("Android") and os_ == "Android 10" and pv_hint.split(".")[0] != "10":
        os_ = f"Android {pv_hint.split('.')[0]}"  # Chrome «замораживает» Android 10 в UA
    if pv_hint and os_ == "Windows 10/11":
        try:
            os_ = "Windows 11" if int(pv_hint.split(".")[0]) >= 13 else "Windows 10"
        except ValueError:
            pass
    if model and not brand:
        brand = brand_of(model)
    browser = "?"
    for rx, name in BROWSERS:
        if b := re.search(rx, ua, re.I if name == "бот/скрипт" else 0):
            ver = next((g for g in b.groups() if g and g.isdigit()), "") if b.groups() else ""
            browser = f"{name} {ver}".strip()
            if name == "бот/скрипт":
                kind = "бот"
            break
    return {"kind": kind, "brand": brand, "model": clip(model, 60), "os": os_, "browser": browser}


def dev_id(sid: str) -> str:
    """Короткий ID устройства для журнала (по нему /who)."""
    return hashlib.sha256(("dev:" + sid).encode()).hexdigest()[:6]


def fmt_time(ts: float, with_date=True) -> str:
    return time.strftime("%d.%m %H:%M" if with_date else "%H:%M", time.gmtime(ts + TZ_SHIFT))


def record(q, sid: str, ip: str, ua: str, lang: str, ref: str, mac: str | None, wifi: bool) -> tuple[int, bool]:
    """Записывает заход. Возвращает (id записи, новая ли это сессия)."""
    now = time.time()
    row = q("SELECT id FROM visits WHERE sid=? AND last>? ORDER BY id DESC LIMIT 1", (sid, now - SESSION_GAP))
    if row:
        q("UPDATE visits SET last=?, hits=hits+1, ip=?, mac=COALESCE(?,mac), wifi=MAX(wifi,?) WHERE id=?",
          (now, ip, mac, int(wifi), row[0][0]))
        return row[0][0], False
    p = parse_ua(ua)
    q("INSERT INTO visits(sid,dev,mac,ip,wifi,ua,kind,brand,model,os,browser,lang,ref,first,last,hits)"
      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
      (sid, dev_id(sid), mac, ip, int(wifi), clip(ua, 400), p["kind"], p["brand"], p["model"], p["os"],
       p["browser"], clip(lang, 40), clip(ref, 200), now, now))
    vid = q("SELECT last_insert_rowid()")[0][0]
    if vid % 200 == 0:
        q("DELETE FROM visits WHERE id <= ?", (vid - KEEP,))
    return vid, True


def hello(q, sid: str, d: dict) -> None:
    """Уточнения от JS страницы: экран, часовой пояс, точная модель (Client Hints)."""
    row = q("SELECT id, ua FROM visits WHERE sid=? ORDER BY id DESC LIMIT 1", (sid,))
    if not row:
        return
    vid, ua = row[0]
    model, pv = clip(d.get("model"), 60), clip(d.get("pv"), 20)
    p = parse_ua(ua, model, pv)
    q("UPDATE visits SET screen=?, tz=?, model=?, brand=?, os=?, lang=COALESCE(NULLIF(?,''),lang) WHERE id=?",
      (clip(d.get("screen"), 30), clip(d.get("tz"), 40), p["model"], p["brand"], p["os"], clip(d.get("lang"), 40), vid))


def line(r: dict, full=False) -> str:
    dev = " ".join(x for x in (r["brand"], r["model"]) if x) or r["kind"]
    parts = [f"🕒 {fmt_time(r['first'])}" + (f"–{fmt_time(r['last'], False)}" if r["last"] - r["first"] > 60 else ""),
             f"🆔 {r['dev']}" + (f" · Анон{r['num']}" if r.get("num") else "") + (f" «{r['name']}»" if r.get("name") else ""),
             f"📱 {dev} · {r['os']}", f"🌐 {r['browser']}"]
    net = f"📶 {'Wi-Fi точки' if r['wifi'] else 'не с нашей точки'} · IP {r['ip']}"
    if r["mac"]:
        net += f" · MAC {r['mac']}"
    parts.append(net)
    extra = [x for x in (r["screen"] and f"экран {r['screen']}", r["tz"], r["lang"],
                         r["hits"] > 1 and f"запросов: {r['hits']}") if x]
    if extra:
        parts.append("ℹ️ " + " · ".join(extra))
    if full:
        parts.append(f"UA: {r['ua']}")
    return "\n".join(parts)


COLS = "v.id, v.dev, v.mac, v.ip, v.wifi, v.ua, v.kind, v.brand, v.model, v.os, v.browser, v.lang, v.screen, v.tz, v.first, v.last, v.hits, g.num, g.name"


def rows(q, where="1", args=(), limit=20) -> list[dict]:
    names = [c.split(".")[1] for c in COLS.split(", ")]
    res = q(f"SELECT {COLS} FROM visits v LEFT JOIN guests g ON g.sid=v.sid WHERE {where} ORDER BY v.id DESC LIMIT ?",
            (*args, limit))
    return [dict(zip(names, r)) for r in res]


def stats(q) -> str:
    now = time.time()
    day0 = (now + TZ_SHIFT) // 86400 * 86400 - TZ_SHIFT
    out = []
    for title, since in (("Сегодня", day0), ("7 дней", now - 7 * 86400), ("Всего в журнале", 0)):
        n, u = q("SELECT COUNT(*), COUNT(DISTINCT sid) FROM visits WHERE first>=?", (since,))[0]
        out.append(f"{title}: {n} заходов, {u} устройств")
    for col, title in (("brand", "Марки"), ("os", "Системы"), ("browser", "Браузеры")):
        top = q(f"SELECT COALESCE(NULLIF({col},''),'?'), COUNT(DISTINCT sid) c FROM visits WHERE first>=? "
                f"GROUP BY 1 ORDER BY c DESC LIMIT 6", (now - 7 * 86400,))
        if top:
            out.append(f"\n{title} (7 дней): " + ", ".join(f"{k} {c}" for k, c in top))
    hours = q("SELECT CAST(((first+?)%86400)/3600 AS INT) h, COUNT(*) c FROM visits WHERE first>=? GROUP BY h ORDER BY c DESC LIMIT 3",
              (TZ_SHIFT, now - 7 * 86400))
    if hours:
        out.append("Часы пик: " + ", ".join(f"{h:02d}:00 ({c})" for h, c in hours))
    return "📊 " + "\n".join(out)
