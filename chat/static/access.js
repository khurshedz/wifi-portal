(() => {
  const $ = (id) => document.getElementById(id);
  const card = $("card-net");
  if (!card) return;
  let fp = "", st = null, timer = null, codeOpen = false;

  // отпечаток устройства: не личные данные — один из признаков, по которым узнаём телефон
  const fingerprint = async () => {
    try {
      const c = document.createElement("canvas"); c.width = 200; c.height = 40;
      const x = c.getContext("2d");
      x.textBaseline = "top"; x.font = "16px Arial"; x.fillStyle = "#c94f2c"; x.fillRect(0, 0, 60, 20);
      x.fillStyle = "#123"; x.fillText("Assalom Wi-Fi ✓ 2026", 4, 12);
      const parts = [screen.width + "x" + screen.height + "x" + screen.colorDepth, devicePixelRatio,
        Intl.DateTimeFormat().resolvedOptions().timeZone, navigator.hardwareConcurrency, navigator.deviceMemory,
        navigator.maxTouchPoints, navigator.platform, c.toDataURL()].join("|");
      const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(parts));
      return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
    } catch (e) { return ""; }
  };

  const api = async (path, body) => {
    const r = await fetch(path, {
      method: body ? "POST" : "GET", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "chat" },
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(data.detail || "Ошибка " + r.status);
    return data;
  };

  const mins = (s) => s >= 3600 ? `${Math.floor(s / 3600)} ч ${Math.floor(s % 3600 / 60)} мин` : `${Math.floor(s / 60)} мин`;
  const clock = (s) => s >= 3600
    ? `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
    : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const el = (tag, attrs = {}, text = "") => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text) e.textContent = text;
    return e;
  };
  const body = $("net-body"), err = $("net-err");

  const run = async (path, data, btn) => {
    err.textContent = "";
    if (btn) btn.disabled = true;
    try {
      st = await api(path, { ...data, fp });
      if (st.nav) { location.href = st.nav; return; }  // вход/выход в роутере (Chilli), он вернёт на портал
      render();
    } catch (e) { err.textContent = e.message; }
    finally { if (btn) btn.disabled = false; }
  };

  const bigButton = (title, sub, onClick, disabled) => {
    const b = el("button", { type: "button", class: "net-big" });
    b.append(el("span", { class: "t" }, title), el("span", { class: "s" }, sub));
    b.disabled = !!disabled;
    b.addEventListener("click", () => onClick(b));
    return b;
  };

  const codeBlock = () => {
    const wrap = el("div", { class: "net-code" });
    if (!codeOpen) {
      const link = el("button", { type: "button", class: "link" }, "🎟 Есть код на время?");
      link.addEventListener("click", () => { codeOpen = true; render(); });
      wrap.append(link);
      return wrap;
    }
    const f = el("form", { class: "net-row" });
    const ci = el("input", { placeholder: "XXXXX-XXXXX", maxlength: "11", autocomplete: "off", autocapitalize: "characters" });
    const cb = el("button", { type: "submit" }, "Ввести");
    f.append(ci, cb);
    f.addEventListener("submit", (e) => { e.preventDefault(); run("/api/access/redeem", { code: ci.value }, cb); });
    wrap.append(f);
    setTimeout(() => ci.focus(), 0);
    return wrap;
  };

  const render = () => {
    clearInterval(timer);
    body.replaceChildren();
    if (!st) return;
    if (!st.device_ok) {
      body.append(el("p", { class: "net-hint" }, "Чтобы включить интернет, подключитесь к сети «Assalom Wi-Fi» и откройте эту страницу."));
      return;
    }
    const a = st.active;
    if (a) {
      let left = a.left;
      const big = el("div", { class: "net-left" }, clock(left));
      body.append(el("div", { class: "net-on" }, a.here ? "✅ Интернет включён" : "Интернет включён на другом вашем устройстве"), big,
                  el("div", { class: "net-hint" }, a.mode === "paid" ? "осталось с баланса" : "осталось бесплатных минут"));
      timer = setInterval(() => {
        left = Math.max(0, left - 1); big.textContent = clock(left);
        if (!left) { clearInterval(timer); setTimeout(load, 3000); }
      }, 1000);
      const row = el("div", { class: "net-links" });
      if (a.here) {
        const again = el("button", { type: "button", class: "link" }, "Не открывается сайт?");
        again.addEventListener("click", () => run("/api/access/relogon", {}, again));
        const stop = el("button", { type: "button", class: "link" }, "Остановить");
        stop.addEventListener("click", () => {
          if (confirm("Отключить интернет? Время сверх текущего часа вернётся на счёт.")) run("/api/access/stop", {}, stop);
        });
        row.append(again, stop);
      } else {
        const here = el("button", { type: "button" }, "Включить на этом телефоне");
        here.addEventListener("click", () => run("/api/access/start", { mode: a.mode }, here));
        row.append(here);
      }
      body.append(row);
      return;
    }
    const free = st.free_left || 0;
    if (st.balance >= 60) {
      body.append(bigButton("▶ Включить интернет", `с баланса · ${mins(st.balance)}`,
        (b) => run("/api/access/start", { mode: "paid" }, b)));
      if (free >= 60) {
        const f = el("button", { type: "button", class: "link" }, `или бесплатно · ${mins(free)}`);
        f.addEventListener("click", () => run("/api/access/start", { mode: "free" }, f));
        const row = el("div", { class: "net-links" }); row.append(f); body.append(row);
      }
    } else if (free >= 60) {
      body.append(bigButton("▶ Включить интернет", `бесплатно · осталось ${mins(free)} из ${st.free_total} мин сегодня`,
        (b) => run("/api/access/start", { mode: "free" }, b)));
    } else {
      body.append(bigButton("Бесплатные минуты на сегодня закончились", `завтра снова ${st.free_total} минут · или введите код`, () => {}, true));
    }
    body.append(codeBlock());
  };

  const load = async () => {
    try { st = await api("/api/access?fp=" + encodeURIComponent(fp)); render(); err.textContent = ""; }
    catch (e) { if (/404/.test(e.message)) card.hidden = true; else err.textContent = e.message; }
  };

  card.hidden = false;
  fingerprint().then((h) => { fp = h; load(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
})();
