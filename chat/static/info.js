(() => {
  const $ = (id) => document.getElementById(id);
  const TZ = "Asia/Dushanbe";
  let offset = 0;          // серверное время - время телефона, мс
  let namazTimes = [];

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const now = () => new Date(Date.now() + offset);
  const hm = (d) => d.toLocaleTimeString("ru-RU", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
  const minutesOfDay = (d) => {
    const [h, m] = hm(d).split(":").map(Number);
    return h * 60 + m;
  };
  const toMin = (s) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };

  // WMO weather code -> [иконка день, иконка ночь, описание, класс неба]
  const WMO = (c) => {
    if (c === 0) return ["☀️", "🌙", "Ясно", "clear"];
    if (c <= 2) return ["🌤️", "🌙", "Малооблачно", "clear"];
    if (c === 3) return ["☁️", "☁️", "Пасмурно", "cloudy"];
    if (c <= 48) return ["🌫️", "🌫️", "Туман", "fog"];
    if (c <= 57) return ["🌦️", "🌧️", "Морось", "rain"];
    if (c <= 67) return ["🌧️", "🌧️", "Дождь", "rain"];
    if (c <= 77) return ["🌨️", "🌨️", "Снег", "snow"];
    if (c <= 82) return ["🌦️", "🌧️", "Ливень", "rain"];
    if (c <= 86) return ["🌨️", "🌨️", "Снегопад", "snow"];
    return ["⛈️", "⛈️", "Гроза", "storm"];
  };

  // ---------- часы ----------
  const tick = () => {
    const d = now();
    $("time").textContent = hm(d);
    const ds = d.toLocaleDateString("ru-RU", { timeZone: TZ, weekday: "long", day: "numeric", month: "long" });
    $("date").textContent = ds.charAt(0).toUpperCase() + ds.slice(1);
    highlightNamaz(d);
  };

  // ---------- окно ----------
  const paintSky = (kind, isNight, isDusk) => {
    const sky = $("sky");
    sky.replaceChildren();
    const win = $("window");
    win.className = "window " + (isDusk ? "dusk " : isNight ? "night " : "day ") + kind;
    const rnd = (a, b) => a + Math.random() * (b - a);
    if (isNight && (kind === "clear" || kind === "cloudy")) {
      for (let i = 0; i < 40; i++) {
        const s = el("span", "star");
        s.style.left = rnd(0, 100) + "%"; s.style.top = rnd(0, 70) + "%";
        s.style.animationDelay = rnd(0, 3) + "s";
        sky.appendChild(s);
      }
    }
    if (kind !== "clear") {
      for (let i = 0; i < 3; i++) {
        const c = el("span", "cloud", "☁️");
        c.style.top = rnd(0, 40) + "%"; c.style.animationDuration = rnd(40, 80) + "s";
        c.style.animationDelay = -rnd(0, 60) + "s";
        sky.appendChild(c);
      }
    }
    if (kind === "rain" || kind === "storm" || kind === "snow") {
      const n = kind === "snow" ? 30 : 50;
      for (let i = 0; i < n; i++) {
        const p = kind === "snow" ? el("span", "flake", "❄") : el("span", "drop");
        p.style.left = rnd(0, 100) + "%";
        p.style.animationDuration = (kind === "snow" ? rnd(4, 8) : rnd(.6, 1.2)) + "s";
        p.style.animationDelay = -rnd(0, 8) + "s";
        sky.appendChild(p);
      }
    }
  };

  const renderWeather = (w) => {
    if (!w) { $("wx-desc").textContent = "погода недоступна"; return; }
    const [dayIcon, nightIcon, desc, kind] = WMO(w.code);
    const m = minutesOfDay(now());
    const rise = w.sunrise ? toMin(w.sunrise) : 420, set = w.sunset ? toMin(w.sunset) : 1050;
    const isDusk = Math.abs(m - set) < 40 || Math.abs(m - rise) < 30;
    const isNight = !w.is_day;
    $("wx-icon").textContent = isNight ? nightIcon : dayIcon;
    $("wx-temp").textContent = (w.temp > 0 ? "+" : "") + w.temp + "°";
    $("wx-desc").textContent = desc + ", ощущается " + (w.feels > 0 ? "+" : "") + w.feels + "°";
    const extra = $("wx-extra");
    extra.replaceChildren(
      el("span", null, "↑" + w.max + "° ↓" + w.min + "°"),
      el("span", null, "💧 " + w.humidity + "%"),
      el("span", null, "🌬 " + w.wind + " м/с"),
    );
    paintSky(kind, isNight, isDusk);

    if (!w.sunrise || !w.sunset) return;
    $("card-sun").hidden = false;
    const len = set - rise;
    $("sun").replaceChildren(
      el("span", null, "Восход"), el("b", null, w.sunrise),
      el("span", null, "Закат"), el("b", null, w.sunset),
      el("span", null, "Световой день"), el("b", null, Math.floor(len / 60) + " ч " + (len % 60) + " мин"),
    );
  };

  // ---------- карточки ----------
  const fmt = (v, d = 2) => Number(v).toFixed(d);

  const renderEskhata = (e) => {
    if (!e) return;
    $("card-eskhata").hidden = false;
    $("esh-date").textContent = e.date ? "на " + e.date : "";
    const flags = { USD: "🇺🇸 USD", EUR: "🇪🇺 EUR", RUB: "🇷🇺 RUB" };
    const body = $("esh-table").querySelector("tbody");
    body.replaceChildren();
    for (const [cur, r] of Object.entries(e.rates)) {
      const tr = el("tr");
      const d = cur === "RUB" ? 4 : 2;
      tr.append(el("td", null, flags[cur] || cur), el("td", null, fmt(r.buy, d)), el("td", null, fmt(r.sell, d)));
      body.appendChild(tr);
    }
    if (e.transfer_rub) {
      $("esh-transfer").textContent = "Переводы из России: 1000 ₽ ≈ " + fmt(e.transfer_rub.buy * 1000) + " сомони";
    }
  };

  const renderCbr = (c) => {
    if (!c) return;
    $("card-cbr").hidden = false;
    $("cbr").replaceChildren(
      el("span", null, "1 $"), el("b", null, fmt(c.usd_tjs) + " сом."),
      el("span", null, "1 $"), el("b", null, fmt(c.usd_rub) + " ₽"),
      el("span", null, "1000 ₽"), el("b", null, fmt(c.rub1000_tjs) + " сом."),
    );
  };

  const renderNamaz = (n) => {
    if (!n) return;
    $("card-namaz").hidden = false;
    $("hijri").textContent = n.hijri;
    namazTimes = n.times;
    const box = $("namaz");
    box.replaceChildren(...n.times.map((t) => {
      const d = el("div"); d.append(el("span", null, t.name), el("b", null, t.time)); return d;
    }));
    highlightNamaz(now());
  };

  function highlightNamaz(d) {
    if (!namazTimes.length) return;
    const m = minutesOfDay(d);
    const prayers = namazTimes.map((t, i) => ({ ...t, i, min: toMin(t.time) })).filter((t) => t.name !== "Восход");
    let next = prayers.find((t) => t.min > m) || prayers[0];
    const cells = $("namaz").children;
    for (let i = 0; i < cells.length; i++) cells[i].classList.toggle("next-up", i === next.i);
    let left = next.min - m; if (left < 0) left += 1440;
    $("namaz-next").textContent = "До " + next.name + ": " + (left >= 60 ? Math.floor(left / 60) + " ч " : "") + (left % 60) + " мин";
  }

  const AQI = [[50, "🟢", "Хороший"], [100, "🟡", "Умеренный"], [150, "🟠", "Вреден для чувствительных"], [200, "🔴", "Вредный"], [300, "🟣", "Очень вредный"]];
  const renderAir = (a) => {
    if (!a) return;
    $("card-air").hidden = false;
    const [, e, txt] = AQI.find(([lim]) => a.aqi <= lim) || [0, "🟤", "Опасный"];
    $("air").textContent = e + " " + a.aqi;
    $("air-desc").textContent = txt + " · PM2.5 " + fmt(a.pm25, 1);
  };

  const renderMag = (g) => {
    if (!g) return;
    $("card-mag").hidden = false;
    const kp = g.kp;
    const [e, txt] = kp < 4 ? ["🟢", "Спокойно"] : kp < 5 ? ["🟡", "Возмущённо"] : kp < 6 ? ["🟠", "Слабая буря"] : ["🔴", "Магнитная буря"];
    $("mag").textContent = e + " Kp " + fmt(kp, 1);
    $("mag-desc").textContent = txt;
  };

  const load = async () => {
    try {
      const r = await fetch("/api/info", { credentials: "same-origin" });
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      offset = d.now * 1000 - Date.now();
      tick();
      renderWeather(d.weather);
      renderEskhata(d.eskhata);
      renderCbr(d.cbr);
      renderNamaz(d.namaz);
      renderAir(d.air);
      renderMag(d.magnetic);
    } catch (e) {
      $("wx-desc").textContent = "нет данных, повторим…";
    }
  };

  tick();
  setInterval(tick, 1000);
  load();
  setInterval(load, 10 * 60 * 1000);
})();
