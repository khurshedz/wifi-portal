(() => {
  const $ = (id) => document.getElementById(id);
  const log = $("log"), form = $("form"), text = $("text"), nameEl = $("name"),
        err = $("err"), me = $("me"), sendBtn = $("send"), emojis = $("emojis");
  const EMOJI = "😀😂😊😍🥰😎🤔😢😡👍👎🙏👋💪🔥❤️🎉✅❌⭐☕🍀🌸📶".match(/\p{Extended_Pictographic}️?/gu);
  let last = 0, polling = false;

  try { nameEl.value = localStorage.getItem("name") || ""; } catch (e) {}

  const api = async (path, opts = {}) => {
    const r = await fetch(path, {
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
      const d = await api("/api/send", { method: "POST", body: JSON.stringify({ text: value, name: name || null }) });
      text.value = "";
      showMe(d.num, name);
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

  api("/api/me").then((d) => showMe(d.num, nameEl.value.trim())).catch(() => {});
  poll();
  setInterval(poll, 3000);
})();
