(() => {
  const $ = (id) => document.getElementById(id);
  const card = $("card-net");
  if (!card) return;
  let fp = "", st = null, timer = null;

  // отпечаток устройства: не секрет и не личные данные — только помогает не раздавать бесплатные минуты повторно
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

  const mins = (s) => s >= 3600 ? `${Math.floor(s / 3600)} ч ${Math.floor(s % 3600 / 60)} мин` : `${Math.ceil(s / 60)} мин`;
  const clock = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const el = (tag, attrs = {}, text = "") => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text) e.textContent = text;
    return e;
  };
  const body = $("net-body"), err = $("net-err");
  const fail = (e) => { err.textContent = e.message || String(e); };

  const run = async (path, data, btn) => {
    err.textContent = "";
    if (btn) btn.disabled = true;
    try {
      st = await api(path, data);
      if (st.nav) { location.href = st.nav; return; }  // вход/выход в роутере (Chilli), он вернёт на портал
      render();
    }
    catch (e) { fail(e); }
    finally { if (btn) btn.disabled = false; }
  };

  const authForm = () => {
    const f = el("form", { class: "net-form", autocomplete: "on" });
    const l = el("input", { name: "username", autocomplete: "username", placeholder: "Логин (латиница)", maxlength: "20", required: "" });
    const p = el("input", { name: "password", type: "password", autocomplete: "current-password", placeholder: "Пароль", maxlength: "64", required: "" });
    const bIn = el("button", { type: "submit" }, "Войти");
    const bReg = el("button", { type: "button", class: "ghost" }, "Создать аккаунт");
    f.append(l, p, el("div", { class: "net-row" }));
    f.lastChild.append(bIn, bReg);
    f.addEventListener("submit", (e) => { e.preventDefault(); run("/api/access/login", { login: l.value, password: p.value, fp }, bIn); });
    bReg.addEventListener("click", () => {
      if (!f.reportValidity()) return;
      run("/api/access/register", { login: l.value, password: p.value, fp }, bReg);
    });
    return f;
  };

  const render = () => {
    clearInterval(timer);
    body.replaceChildren();
    if (!st) return;
    if (!st.logged) {
      body.append(el("p", { class: "muted small" },
        `Войдите или создайте аккаунт: ${st.free_total} минут интернета в день бесплатно. Время привязано к аккаунту и одному устройству.`));
      if (!st.device_ok) body.append(el("p", { class: "muted small" }, "Чтобы включить интернет, подключитесь к сети «Assalom Wi-Fi»."));
      body.append(authForm());
      return;
    }
    const head = el("div", { class: "net-head" });
    head.append(el("b", {}, "👤 " + st.login));
    const out = el("button", { type: "button", class: "link" }, "выйти");
    out.addEventListener("click", () => run("/api/access/logout", {}, out));
    head.append(out);
    body.append(head);
    if (st.active) {
      const a = st.active;
      let left = a.left;
      const big = el("div", { class: "net-left" }, clock(left));
      body.append(el("div", { class: "muted small" }, a.mode === "paid" ? "Интернет включён · с баланса" : "Интернет включён · бесплатные минуты"), big);
      if (!a.here) body.append(el("p", { class: "muted small" }, "Включено на другом вашем устройстве. Нажмите «Включить здесь», чтобы перенести."));
      timer = setInterval(() => {
        left = Math.max(0, left - 1); big.textContent = clock(left);
        if (!left) { clearInterval(timer); setTimeout(load, 3000); }
      }, 1000);
      const row = el("div", { class: "net-row" });
      if (a.here) {
        const again = el("button", { type: "button" }, "🔄 Не открывается? Подключить снова");
        again.addEventListener("click", () => run("/api/access/relogon", {}, again));
        const stop = el("button", { type: "button", class: "ghost" }, "⏸ Остановить");
        stop.addEventListener("click", () => run("/api/access/stop", {}, stop));
        row.append(again, stop);
      } else {
        const here = el("button", { type: "button" }, "Включить здесь");
        here.addEventListener("click", () => run("/api/access/start", { mode: a.mode, fp }, here));
        row.append(here);
      }
      body.append(row);
    } else {
      const info = el("div", { class: "net-info" });
      info.append(el("div", {}, `Бесплатно сегодня: ${st.free_left == null ? "—" : mins(st.free_left)}`),
                  el("div", {}, `На балансе: ${mins(st.balance)}`));
      body.append(info);
      const row = el("div", { class: "net-row" });
      const free = el("button", { type: "button" }, "▶ Бесплатно");
      free.disabled = !st.device_ok || !st.free_left || st.free_left < 60;
      free.addEventListener("click", () => run("/api/access/start", { mode: "free", fp }, free));
      const paid = el("button", { type: "button", class: "ghost" }, "▶ С баланса");
      paid.disabled = !st.device_ok || st.balance < 60;
      paid.addEventListener("click", () => run("/api/access/start", { mode: "paid", fp }, paid));
      row.append(free, paid);
      body.append(row);
      if (!st.device_ok) body.append(el("p", { class: "muted small" }, "Включить можно только из сети «Assalom Wi-Fi»."));
    }
    const cf = el("form", { class: "net-row net-code" });
    const ci = el("input", { placeholder: "Код пополнения XXXXX-XXXXX", maxlength: "11", autocomplete: "off" });
    const cb = el("button", { type: "submit", class: "ghost" }, "Ввести");
    cf.append(ci, cb);
    cf.addEventListener("submit", (e) => { e.preventDefault(); run("/api/access/redeem", { code: ci.value }, cb); });
    body.append(cf);
  };

  const load = async () => {
    try { st = await api("/api/access?fp=" + encodeURIComponent(fp)); render(); err.textContent = ""; }
    catch (e) { if (/404/.test(e.message)) card.hidden = true; else fail(e); }
  };

  card.hidden = false;
  fingerprint().then((h) => { fp = h; load(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
})();
