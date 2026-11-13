"""Инфо-блок для гостей: погода в Турсунзаде, курсы (Эсхата, ЦБ), намаз, воздух, магнитосфера.

Гости без интернета — всё собирает сервер и кеширует. Источники взяты по мотивам ~/PycharmProjects/telegram_bot.
Упавший источник просто пропускается, остальные отдаются.
"""
import asyncio
import html
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

LAT, LON = 38.5108, 68.2303  # Турсунзаде
TZ = "Asia/Dushanbe"
UA = {"User-Agent": "Mozilla/5.0 (wifi-portal info)"}

_cache: dict[str, tuple[float, object]] = {}
_locks: dict[str, asyncio.Lock] = {}
errors: dict[str, str] = {}  # последняя ошибка источника — для диагностики


async def cached(key: str, ttl: int, fetch, client: httpx.AsyncClient):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    async with _locks.setdefault(key, asyncio.Lock()):
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = await fetch(client)
        except Exception as e:
            print(f"info {key} error:", e)
            errors[key] = f"{type(e).__name__}: {str(e)[:80]}"
            # отдаём устаревшее, если есть; повторим попытку через минуту
            if hit:
                _cache[key] = (time.time() - ttl + 60, hit[1])
                return hit[1]
            return None
        _cache[key] = (time.time(), val)
        errors.pop(key, None)
        return val


async def weather(c):
    r = await c.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": LAT, "longitude": LON, "timezone": TZ, "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
        "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min",
    }, timeout=15)
    d = r.json()
    cur, day = d["current"], d["daily"]
    return {
        "temp": round(cur["temperature_2m"]), "feels": round(cur["apparent_temperature"]),
        "humidity": cur["relative_humidity_2m"], "wind": round(cur["wind_speed_10m"] / 3.6, 1),
        "code": cur["weather_code"], "is_day": bool(cur["is_day"]),
        "max": round(day["temperature_2m_max"][0]), "min": round(day["temperature_2m_min"][0]),
        "sunrise": day["sunrise"][0][-5:], "sunset": day["sunset"][0][-5:],
    }


MET_UA = {"User-Agent": "wifi-portal/1.0 github.com/khurshedz/wifi-portal"}
MET_CODES = [  # symbol_code met.no -> WMO-подобный код для фронта
    ("thunder", 95), ("snowshowers", 85), ("snow", 73), ("sleet", 67), ("rainshowers", 80),
    ("heavyrain", 65), ("lightrain", 61), ("rain", 63), ("fog", 45), ("partlycloudy", 2),
    ("cloudy", 3), ("fair", 1), ("clearsky", 0),
]


async def weather_met(c):
    """Запасной источник: met.no (Open-Meteo режет общие IP облачных хостингов)."""
    r = await c.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                    params={"lat": round(LAT, 2), "lon": round(LON, 2)}, headers=MET_UA, timeout=15)
    ts = r.json()["properties"]["timeseries"]
    now = ts[0]["data"]
    det = now["instant"]["details"]
    sym = (now.get("next_1_hours") or now.get("next_6_hours") or {}).get("summary", {}).get("symbol_code", "cloudy")
    code = next((v for k, v in MET_CODES if sym.startswith(k)), 3)
    temps = [p["data"]["instant"]["details"]["air_temperature"] for p in ts[:24]]
    t, wind = det["air_temperature"], det["wind_speed"]
    # «ощущается»: охлаждение ветром (для холода), иначе как есть
    feels = 13.12 + 0.6215 * t - 11.37 * (wind * 3.6) ** 0.16 + 0.3965 * t * (wind * 3.6) ** 0.16 \
        if t <= 10 and wind > 1.3 else t
    return {
        "temp": round(t), "feels": round(feels), "humidity": round(det["relative_humidity"]),
        "wind": round(wind, 1), "code": code, "is_day": None if "_" not in sym else sym.endswith("_day"),
        "max": round(max(temps)), "min": round(min(temps)), "sunrise": None, "sunset": None,
    }


async def weather_any(c):
    try:
        return await weather(c)
    except Exception as e:
        print("open-meteo failed, using met.no:", e)
        return await weather_met(c)


async def air(c):
    r = await c.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
        "latitude": LAT, "longitude": LON, "timezone": TZ, "current": "us_aqi,pm2_5"}, timeout=15)
    cur = r.json()["current"]
    return {"aqi": cur["us_aqi"], "pm25": cur["pm2_5"]}


async def eskhata(c):
    r = await c.get("https://eskhata.com/", headers=UA, timeout=20, follow_redirects=True)
    s = re.sub(r"<svg.*?</svg>|<script.*?</script>", "", r.text, flags=re.S)
    s = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)))
    date = re.search(r"Курсы валют на (\d\d\.\d\d\.\d{4})", s)
    # таблицы «Банк покупает / продает / НБТ»: [0] частным лицам, [1] юрлицам, [2] денежные переводы
    blocks = s.split("Валюта Банк покупает Банк продает Курс НБТ")[1:]

    def parse(block):
        out = {}
        for cur, buy, sell, nbt in re.findall(r"\b([A-Z]{3}) (\d+\.\d+) (\d+\.\d+) (\d+\.\d+)", block):
            out.setdefault(cur, {"buy": float(buy), "sell": float(sell), "nbt": float(nbt)})
        return out

    private = parse(blocks[0].split("Валюта Курс погашения")[0])
    transfers = parse(blocks[2]) if len(blocks) > 2 else {}
    if "USD" not in private:
        raise ValueError("eskhata: таблица не найдена")
    return {"date": date.group(1) if date else None,
            "rates": {k: private[k] for k in ("USD", "EUR", "RUB") if k in private},
            "transfer_rub": transfers.get("RUB")}


async def cbr(c):
    j = (await c.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=15)).json()
    v, r_date = j["Valute"], j.get("Date", "")[:10]
    rub_usd = v["USD"]["Value"]
    rub_tjs = v["TJS"]["Value"] / v["TJS"]["Nominal"]
    rub_eur = v["EUR"]["Value"]
    return {"usd_tjs": round(rub_usd / rub_tjs, 2), "usd_rub": round(rub_usd, 2),
            "eur_tjs": round(rub_eur / rub_tjs, 2), "eur_rub": round(rub_eur, 2),
            "rub1000_tjs": round(1000 / rub_tjs, 2), "tjs_rub": round(rub_tjs, 2),
            "date": r_date}


async def namaz(c):
    r = await c.get("https://api.aladhan.com/v1/timings", params={
        "latitude": LAT, "longitude": LON, "method": 3, "school": 1, "timezonestring": TZ},
        timeout=15, follow_redirects=True)
    d = r.json()["data"]
    t = d["timings"]
    h = d["date"]["hijri"]
    names = [("Фаджр", "Fajr"), ("Восход", "Sunrise"), ("Зухр", "Dhuhr"), ("Аср", "Asr"),
             ("Магриб", "Maghrib"), ("Иша", "Isha")]
    return {"times": [{"name": ru, "time": t[en]} for ru, en in names],
            "hijri": f'{h["day"]} {h["month"]["en"]} {h["year"]}', "date": d["date"]["gregorian"]["date"]}


async def magnetic(c):
    txt = (await c.get("https://kp.gfz.de/fileadmin/files_for_gfz_cms/Kp_ap_nowcast.txt", timeout=15)).text
    rows = [ln.split() for ln in txt.splitlines() if ln and not ln.startswith("#")]
    last = [r for r in rows if float(r[7]) >= 0][-1]
    return {"kp": float(last[7])}


# аят дня: (сура, с аята, по аят) — подборка известных аятов; текст берём из API, сами не пишем
QURAN = [
    (1, 1, 7), (112, 1, 4), (2, 255, 255), (94, 5, 8), (113, 1, 5), (114, 1, 6), (2, 286, 286),
    (13, 28, 28), (103, 1, 3), (65, 2, 3), (2, 152, 153), (108, 1, 3), (3, 139, 139), (39, 53, 53),
    (110, 1, 3), (2, 186, 186), (49, 13, 13), (93, 1, 11), (17, 23, 24), (2, 201, 201), (25, 63, 63),
    (16, 97, 97), (20, 114, 114), (14, 7, 7), (2, 45, 45), (31, 18, 19), (40, 60, 60), (3, 8, 8),
]


def day_index() -> int:
    return (datetime.now(timezone(timedelta(hours=5))).date() - datetime(2000, 1, 1).date()).days


async def quran(c):
    s, a, b = QURAN[day_index() % len(QURAN)]
    r = await c.get(f"https://api.alquran.cloud/v1/surah/{s}/editions/quran-uthmani,ru.kuliev",
                    params={"offset": a - 1, "limit": b - a + 1}, timeout=15, follow_redirects=True)
    ar, ru = r.json()["data"]
    return {
        "day": day_index(), "surah": s, "from": a, "to": b,
        "name_ar": ar["name"], "name_en": ar["englishName"], "name_tr": ar.get("englishNameTranslation"),
        "ayahs": [{"n": x["numberInSurah"], "ar": x["text"], "ru": y["text"]}
                  for x, y in zip(ar["ayahs"], ru["ayahs"])],
    }


SOURCES = {  # ключ: (функция, ttl секунд) — редко, чтобы не злоупотреблять бесплатными API
    "weather": (weather_any, 2 * 3600), "air": (air, 2 * 3600), "eskhata": (eskhata, 2 * 3600),
    "cbr": (cbr, 2 * 3600), "namaz": (namaz, 12 * 3600), "magnetic": (magnetic, 2 * 3600),
    "quran": (quran, 12 * 3600),
}


async def collect(client: httpx.AsyncClient) -> dict:
    keys = list(SOURCES)
    vals = await asyncio.gather(*(cached(k, SOURCES[k][1], SOURCES[k][0], client) for k in keys))
    out = dict(zip(keys, vals))
    # namaz кешируется на 3 ч — сбросить, если наступил новый день
    today = datetime.now(timezone(timedelta(hours=5))).strftime("%d-%m-%Y")
    if out.get("namaz") and out["namaz"].get("date") != today:
        _cache.pop("namaz", None)
        out["namaz"] = await cached("namaz", SOURCES["namaz"][1], namaz, client)
    if out.get("quran") and out["quran"].get("day") != day_index():  # новый день — новый аят
        _cache.pop("quran", None)
        out["quran"] = await cached("quran", SOURCES["quran"][1], quran, client)
    w, n = out.get("weather"), out.get("namaz")
    if w and n and not w.get("sunrise"):
        times = {x["name"]: x["time"] for x in n["times"]}
        w = dict(w, sunrise=times.get("Восход"), sunset=times.get("Магриб"))
        if w.get("is_day") is None and w["sunrise"] and w["sunset"]:
            hm = datetime.now(timezone(timedelta(hours=5))).strftime("%H:%M")
            w["is_day"] = w["sunrise"] <= hm < w["sunset"]
        out["weather"] = w
    out["now"] = time.time()
    out["errors"] = dict(errors)
    return out
