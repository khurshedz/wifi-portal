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
    $("time").textContent = d.toLocaleTimeString("ru-RU", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit" });
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
    if (c.date) $("cbr-date").textContent = "ЦБ РФ, " + c.date.split("-").reverse().join(".");
    const tile = (from, val, unit) => {
      const d = el("div", "tile");
      const v = el("div", "val", val);
      v.appendChild(el("small", null, unit));
      d.append(el("div", "from", from), v);
      return d;
    };
    const tiles = [
      tile("🇺🇸 1 доллар", fmt(c.usd_tjs), "сом."),
      tile("🇺🇸 1 доллар", fmt(c.usd_rub), "₽"),
      tile("🇷🇺 1000 рублей", fmt(c.rub1000_tjs), "сом."),
      tile("🇹🇯 1 сомони", fmt(c.tjs_rub), "₽"),
    ];
    if (c.eur_tjs) tiles.push(tile("🇪🇺 1 евро", fmt(c.eur_tjs), "сом."), tile("🇪🇺 1 евро", fmt(c.eur_rub), "₽"));
    $("cbr").replaceChildren(...tiles);
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
  const aqiInfo = (v) => AQI.find(([lim]) => v <= lim) || [0, "🟤", "Опасный"];
  const renderAir = (a) => {
    if (!a) return;
    $("card-air").hidden = false;
    const [, e, txt] = aqiInfo(a.aqi);
    $("air").textContent = e + " " + a.aqi;
    if (a.points && a.points.length) {
      $("air-desc").replaceChildren(...a.points.map((p) => {
        const [, pe] = aqiInfo(p.aqi);
        return el("div", null, p.name + ": " + pe + " " + p.aqi + " · 🌡 " + p.temp + "°");
      }), el("div", null, txt + " · " + a.source));
    } else {
      $("air-desc").textContent = txt + (a.pm25 != null ? " · PM2.5 " + fmt(a.pm25, 1) : "");
    }
  };

  const renderMag = (g) => {
    if (!g) return;
    $("card-mag").hidden = false;
    const kp = g.kp;
    const [e, txt] = kp < 4 ? ["🟢", "Спокойно"] : kp < 5 ? ["🟡", "Возмущённо"] : kp < 6 ? ["🟠", "Слабая буря"] : ["🔴", "Магнитная буря"];
    $("mag").textContent = e + " Kp " + fmt(kp, 1);
    $("mag-desc").textContent = txt;
  };

  const PROVERBS = [
    "Капля за каплей — и образуется море.",
    "Терпение — ключ к радости.",
    "Сначала думай, потом говори.",
    "Хороший сосед лучше дальнего родственника.",
    "Дерево держится корнями, человек — друзьями.",
    "Кто ищет — тот найдёт.",
    "Слово — серебро, молчание — золото.",
    "Гость в доме — радость в доме.",
    "Не торопись: поспешишь — людей насмешишь.",
    "Один камень стены не построит.",
    "Знание — богатство, которое не украдут.",
    "Где согласие, там и благополучие.",
    "Доброе слово и камень растопит.",
    "Учись у того, кто знает больше тебя.",
    "Труд кормит, а лень портит.",
    "Друг познаётся в беде.",
    "Чистая вода — сладкая вода.",
    "Хлеб — всему голова.",
    "Мир держится на добрых людях.",
    "Без труда не вытащишь и рыбку из пруда.",
    "Тише едешь — дальше будешь.",
    "Сделал дело — гуляй смело.",
    "Уважай старших — и младшие будут уважать тебя.",
    "Утро вечера мудренее.",
    "Красота — в душе человека.",
    "Не откладывай на завтра то, что можно сделать сегодня.",
    "Сила — в единстве.",
    "С хорошим человеком и дорога короче.",
    "Мудрый ищет мудрость, глупый — уже нашёл.",
    "Улыбка — ключ к любому сердцу.",
    "Кто рано встаёт, тому Бог подаёт.",
  ];
  const dayIndex = () => {
    const d = now().toLocaleDateString("en-CA", { timeZone: TZ });   // YYYY-MM-DD
    return Math.floor(Date.parse(d) / 86400000);
  };
  const renderProverb = () => { $("proverb").textContent = PROVERBS[dayIndex() % PROVERBS.length]; };

  const AR_DIGITS = "٠١٢٣٤٥٦٧٨٩";
  const arNum = (n) => String(n).replace(/\d/g, (d) => AR_DIGITS[d]);
  const renderQuran = (q) => {
    if (!q || !q.ayahs || !q.ayahs.length) return;
    $("card-quran").hidden = false;
    const range = q.from === q.to ? q.from : q.from + "–" + q.to;
    $("quran-ref").textContent = q.name_en + " " + q.surah + ":" + range;
    $("quran-body").replaceChildren(...q.ayahs.map((a) => {
      const box = el("div", "ayah");
      const ar = el("div", "ar", a.ar);
      ar.lang = "ar"; ar.dir = "rtl";
      ar.appendChild(el("span", "n", "﴿" + arNum(a.n) + "﴾"));
      box.append(ar, el("div", "ru", a.n + ". " + a.ru));
      return box;
    }));
  };

  let photos = [], shownPhotos = "";
  const showCaption = (i) => {
    const p = photos[i];
    if (!p) return;
    $("photo-date").textContent = p.date;
    $("caption").textContent = p.title;
    $("credit").textContent = [p.author, p.license].filter(Boolean).join(" · ") + " · Wikimedia Commons";
    [...$("dots").children].forEach((d, k) => d.classList.toggle("on", k === i));
  };
  const current = () => Math.round($("slides").scrollLeft / Math.max(1, $("slides").clientWidth));
  const go = (i) => {
    const s = $("slides"), n = photos.length;
    s.scrollTo({ left: ((i + n) % n) * s.clientWidth });
  };
  const renderWiki = (w) => {
    if (!w) return;
    if (w.photos && w.photos.length) {
      const sig = w.photos.map((p) => p.id).join();
      if (sig !== shownPhotos) {            // не перерисовывать, если фото те же
        shownPhotos = sig;
        photos = w.photos;
        $("card-photos").hidden = false;
        $("slides").replaceChildren(...photos.map((p, i) => {
          const s = el("div", "slide");
          const img = el("img");
          img.src = "/media/" + p.id; img.alt = p.title; img.loading = i ? "lazy" : "eager";
          s.appendChild(img);
          return s;
        }));
        $("dots").replaceChildren(...photos.map(() => el("span")));
        showCaption(0);
      }
    }
    if (w.onthisday && w.onthisday.length) {
      $("card-otd").hidden = false;
      $("otd").replaceChildren(...w.onthisday.map((o) => {
        const li = el("li");
        li.append(el("b", null, o.year), document.createTextNode(o.text));
        return li;
      }));
    }
  };
  let scrollT;
  $("slides").addEventListener("scroll", () => { clearTimeout(scrollT); scrollT = setTimeout(() => showCaption(current()), 80); });
  $("prev").addEventListener("click", () => go(current() - 1));
  $("next").addEventListener("click", () => go(current() + 1));

  const renderNews = (text) => {
    const box = $("news");
    if (!text) { box.hidden = true; return; }
    $("news-text").textContent = text;
    box.hidden = false;
  };
  const renderOnline = (n) => {
    if (!n) { $("online").textContent = ""; return; }
    const w = n % 10 === 1 && n % 100 !== 11 ? "гость" : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20)) ? "гостя" : "гостей";
    $("online").textContent = "на сайте: " + n + " " + w;
  };

  let updatedAt = 0, nextAt = 0, reloading = false;
  const renderRefresh = () => {
    if (!updatedAt) return;
    const n = now().getTime();
    let left = Math.max(0, Math.round((nextAt - n) / 1000));
    const mm = String(Math.floor(left / 60)).padStart(2, "0"), ss = String(left % 60).padStart(2, "0");
    $("refresh").textContent = "🔄 Данные обновлены в " + hm(new Date(updatedAt)) +
      " · следующее обновление через " + (left >= 3600 ? Math.floor(left / 3600) + " ч " : "") +
      (left >= 3600 ? String(Math.floor((left % 3600) / 60)).padStart(2, "0") + " мин" : mm + ":" + ss);
    if (left === 0 && !reloading) { reloading = true; setTimeout(() => { load().finally(() => { reloading = false; }); }, 3000); }
  };

  const load = async () => {
    try {
      const r = await fetch("/api/info", { credentials: "same-origin" });
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      offset = d.now * 1000 - Date.now();
      if (d.updated) { updatedAt = d.updated * 1000; nextAt = d.next_update * 1000; }
      tick();
      renderWeather(d.weather);
      renderEskhata(d.eskhata);
      renderCbr(d.cbr);
      renderNamaz(d.namaz);
      renderAir(d.air);
      renderMag(d.magnetic);
      renderNews(d.news);
      renderQuran(d.quran);
      renderWiki(d.wiki);
      renderOnline(d.online);
      renderProverb();
    } catch (e) {
      $("wx-desc").textContent = "нет данных, повторим…";
    }
  };

  tick();
  setInterval(() => { tick(); renderRefresh(); }, 1000);
  load();
  renderProverb();
  setInterval(load, 60 * 1000);   // данные всё равно из кеша сервера; часто — ради объявлений и счётчика
})();
