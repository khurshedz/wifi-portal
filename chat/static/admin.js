(() => {
  const $ = (s) => document.querySelector(s);
  const TZ = 5 * 3600;  // Душанбе
  const hm = (t) => { const d = new Date((t + TZ) * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`; };
  const day = (t) => { const d = new Date((t + TZ) * 1000); return `${String(d.getUTCDate()).padStart(2, "0")}.${String(d.getUTCMonth() + 1).padStart(2, "0")}`; };
  const dur = (s) => { s = Math.max(0, Math.round(s)); if (s < 60) return `${s} с`; const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h} ч ${m} мин` : `${m} мин`; };
  const bytes = (b) => { b = b || 0; if (b < 1024) return `${b} Б`; const u = ["КБ", "МБ", "ГБ", "ТБ"]; let i = -1; do { b /= 1024; i++; } while (b >= 1024 && i < 3); return `${b.toFixed(b < 10 ? 1 : 0)} ${u[i]}`; };
  const rate = (bps) => bps > 0 ? `${(bps * 8 / 1e6).toFixed(bps * 8 < 1e7 ? 2 : 1)} Мбит/с` : "—";
  const CAUSE = { idle: "ушёл (простой)", timeout: "кончилось время", user: "вышел сам", lost: "связь потеряна", admin: "отключён", nas: "перезагрузка роутера", stop: "завершён", "в сети": "ещё в сети", "нет данных": "нет данных" };
  const MODE = { free: "бесплатно", paid: "с баланса" };

  const td = (text, cls, sub) => {
    const e = document.createElement("td");
    if (cls) e.className = cls;
    e.append(document.createTextNode(text ?? ""));
    if (sub) { const s = document.createElement("span"); s.className = "sub"; s.textContent = sub; e.append(s); }
    return e;
  };
  const fill = (sel, rows, cols, empty) => {
    const tb = $(sel + " tbody"); tb.replaceChildren();
    if (!rows.length) { const tr = document.createElement("tr"); const c = td(empty, "empty"); c.colSpan = cols; tr.append(c); tb.append(tr); return; }
    for (const cells of rows) { const tr = document.createElement("tr"); tr.append(...cells); tb.append(tr); }
  };
  const badge = (text, cls) => { const e = td(""); const b = document.createElement("span"); b.className = "badge " + cls; b.textContent = text; e.append(b); return e; };

  const render = (d) => {
    const tiles = [[d.online.length, "в сети сейчас"], [d.today.people, "людей сегодня"], [dur(d.today.secs), "времени в сети сегодня"], [bytes(d.today.bytes), "трафика сегодня"]];
    $("#tiles").replaceChildren(...tiles.map(([v, k]) => {
      const t = document.createElement("div"); t.className = "tile";
      const a = document.createElement("div"); a.className = "v"; a.textContent = v;
      const b = document.createElement("div"); b.className = "k"; b.textContent = k;
      t.append(a, b); return t;
    }));
    $("#on-n").textContent = d.online.length ? `· ${d.online.length}` : "";
    fill("#t-online", d.online.map((o) => [
      td(o.user, "", MODE[o.mode] || ""), td(o.device || "—", "", o.mac + (o.ip ? " · " + o.ip : "")),
      td(dur(d.now - o.since), "", "с " + hm(o.since)), td(o.portal ? dur(o.left) : "—"),
      td(o.router ? bytes(o.down) : "—", "r"), td(o.router ? bytes(o.up) : "—", "r"), td(o.router ? rate(o.rate_down) : "—", "r"),
      o.portal && o.router ? badge("в сети", "ok") : o.portal ? badge("ждём роутер", "warn")
        : badge("отключён на портале, роутер ещё пускает", "bad"),
    ]), 8, "Сейчас никого");
    fill("#t-hist", d.history.map((h) => [
      td(h.user, "", MODE[h.mode] || ""), td(h.device || "—", "", h.mac),
      td(hm(h.start), "", day(h.start)), td(h.cause === "в сети" ? "—" : hm(h.end)),
      td(dur(h.secs), "r"), td(bytes(h.down + h.up), "r", `↓${bytes(h.down)} ↑${bytes(h.up)}`), td(CAUSE[h.cause] || h.cause),
    ]), 7, "За сутки сеансов нет (данные появятся после следующего входа гостя)");
    fill("#t-users", d.users.map((u) => [
      td(u.user + (u.blocked ? " ⛔" : ""), u.blocked ? "bad" : ""), td(dur(u.today_secs), "r", u.today_n ? `${u.today_n} сеанс.` : ""),
      td(bytes(u.today_bytes), "r"), td(dur(u.week_secs), "r", bytes(u.week_bytes)), td(dur(u.balance), "r"), td(dur(u.free_left), "r"),
      td(u.seen ? `${day(u.seen)} ${hm(u.seen)}` : "—"), td(u.devices.join(", ") || "—"),
    ]), 8, "Пользователей пока нет");
    $("#upd").textContent = "обновлено " + hm(d.now) + " (Душанбе)";
  };

  const load = async () => {
    try {
      const r = await fetch("/api/admin", { credentials: "same-origin", headers: { "X-Requested-With": "chat" } });
      if (r.status === 404) { $("#err").textContent = "Вход истёк — возьмите новую ссылку командой /dash в Telegram"; $("#auto").checked = false; return; }
      if (!r.ok) throw new Error("Ошибка " + r.status);
      render(await r.json()); $("#err").textContent = "";
    } catch (e) { $("#err").textContent = e.message; }
  };
  load();
  setInterval(() => { if ($("#auto").checked && !document.hidden) load(); }, 10000);
})();
