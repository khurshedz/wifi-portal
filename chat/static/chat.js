(() => {
  const $ = (id) => document.getElementById(id);
  const BASE = document.documentElement.dataset.api || "";  // на роутере: /cgi-bin/portal
  const log = $("log"), form = $("form"), text = $("text"), nameEl = $("name"),
        err = $("err"), me = $("me"), sendBtn = $("send"), emojis = $("emojis");
  const EMOJI = "😀 😃 😄 😁 😆 😅 😂 🤣 😊 😇 🙂 😉 😌 😍 🥰 😘 😗 😋 😛 😜 🤪 😝 🤗 🤭 🤫 🤔 🤐 😐 😑 😶 😏 😒 🙄 😬 😴 🤤 😷 🤒 🤕 🥵 🥶 😎 🤓 🧐 😕 😟 🙁 😮 😯 😲 😳 🥺 😢 😭 😱 😖 😣 😞 😓 😩 😫 😤 😡 😠 🤬 😈 💀 🤡 👻 👽 🤖 💩 👍 👎 👌 ✌️ 🤞 🤟 🤘 👏 🙌 👐 🤝 🙏 💪 👋 🤙 ✍️ 👀 🫶 ❤️ 🧡 💛 💚 💙 💜 🖤 🤍 💔 💕 💖 💯 💥 💫 💬 💤 ✨ 🔥 ⭐ 🌟 ⚡ 🎉 🎊 🎁 🎂 🍰 ☕ 🍵 🥤 🍉 🍇 🍎 🍑 🍒 🍋 🌰 🍞 🍕 🍔 🥗 🍲 🥟 🍚 🍖 🍗 🌸 🌹 🌷 🌼 🌻 🍀 🌿 🌳 🌙 ☀️ 🌤️ ☁️ 🌧️ ❄️ 🌈 🏔️ 🌊 🐱 🐶 🐴 🐑 🐓 🦅 🐟 🦋 ⚽ 🏀 🏆 🥇 🎵 🎶 📱 💻 📶 📷 🚗 🚕 🏠 🕌 🕋 ⏰ 💰 💵 ✅ ❌ ❓ ❗ 🆗 🆘 🇹🇯 🇷🇺 🇺🇿 🇰🇿 🇰🇬 🇹🇷".split(" ");
  let last = 0, polling = false;
  // общий чат гостей — есть только на роутере (BASE задан)
  const roomLog = $("room-log");
  let mode = "admin", roomLast = 0, roomPolling = false;

  try { nameEl.value = localStorage.getItem("name") || ""; } catch (e) {}

  const api = async (path, opts = {}) => {
    const r = await fetch(BASE + path, {
      ...opts, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "chat" },
    });
    if (!r.ok) {
      let msg = "Ошибка " + r.status;
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      throw new Error(msg);
    }
    return r.json();
  };

  const add = (m) => {
    const hint = log.querySelector(".hint");
    if (hint) hint.remove();
    const div = document.createElement("div");
    div.className = "msg " + (m.out ? "them" : "me");
    div.textContent = m.text;                       // только textContent — без HTML
    const t = document.createElement("time");
    t.textContent = new Date(m.ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    div.appendChild(t);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  };

  const addRoom = (m, you) => {
    const hint = roomLog.querySelector(".hint");
    if (hint) hint.remove();
    const div = document.createElement("div");
    const mine = m.u === you;
    div.className = "msg " + (mine ? "me" : "them");
    if (!mine) {
      const who = document.createElement("b");
      who.className = "who";
      who.textContent = m.name || "Анон" + m.num;
      div.appendChild(who);
    }
    div.appendChild(document.createTextNode(m.text));
    const t = document.createElement("time");
    t.textContent = new Date(m.ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    div.appendChild(t);
    roomLog.appendChild(div);
    roomLog.scrollTop = roomLog.scrollHeight;
  };
  const pollRoom = async () => {
    if (roomPolling || !BASE) return;
    roomPolling = true;
    try {
      const d = await api("/api/room?after=" + roomLast);
      d.messages.forEach((m) => { addRoom(m, d.you); roomLast = Math.max(roomLast, m.id); });
    } catch (e) { /* повторим */ }
    roomPolling = false;
  };
  if (BASE) {
    $("tabs").hidden = false;
    $("tabs").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      mode = b.dataset.t;
      [...$("tabs").children].forEach((x) => x.classList.toggle("on", x === b));
      roomLog.hidden = mode !== "room";
      log.hidden = mode === "room";
      if (mode === "room") pollRoom();
    });
    setInterval(pollRoom, 3000);
  }

  const showMe = (num, name) => {
    me.textContent = num ? (name ? "" : "вы: Анон" + num) : "";
  };

  const poll = async () => {
    if (polling) return;
    polling = true;
    try {
      const d = await api("/api/messages?after=" + last);
      d.messages.forEach((m) => { add(m); last = Math.max(last, m.id); });
    } catch (e) { /* тихо, повторим */ }
    polling = false;
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = text.value.trim();
    if (!value) return;
    err.textContent = "";
    sendBtn.disabled = true;
    const name = nameEl.value.trim();
    try { localStorage.setItem("name", name); } catch (e2) {}
    try {
      if (mode === "room") {
        await api("/api/room", { method: "POST", body: JSON.stringify({ text: value, name: name || null }) });
        text.value = "";
        await pollRoom();
        return;
      }
      const d = await api("/api/send", { method: "POST", body: JSON.stringify({ text: value, name: name || null }) });
      text.value = "";
      showMe(d.num, name);
      if (d.queued) {  // нет интернета: сообщение ждёт на роутере
        add({ out: 0, text: value + "  📴 отправим, когда появится интернет", ts: Date.now() / 1000 });
      }
      await poll();
    } catch (e3) {
      err.textContent = e3.message;
    } finally {
      sendBtn.disabled = false;
      text.focus();
    }
  });

  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  text.addEventListener("input", () => { text.style.height = "auto"; text.style.height = text.scrollHeight + "px"; });

  EMOJI.forEach((ch) => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = ch;
    b.addEventListener("click", () => {
      const s = text.selectionStart ?? text.value.length;
      text.value = text.value.slice(0, s) + ch + text.value.slice(text.selectionEnd ?? s);
      text.focus(); text.selectionStart = text.selectionEnd = s + ch.length;
    });
    emojis.appendChild(b);
  });
  $("emoji-btn").addEventListener("click", () => { emojis.hidden = !emojis.hidden; });

  $("copy-jump").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    try { await navigator.clipboard.writeText("opera://jump"); b.textContent = "Скопировано ✓"; }
    catch (err) { b.textContent = "opera://jump"; }
    setTimeout(() => { b.textContent = "Копировать"; }, 2500);
  });

  api("/api/me").then((d) => showMe(d.num, nameEl.value.trim())).catch(() => {});
  poll();
  setInterval(poll, 3000);
})();
