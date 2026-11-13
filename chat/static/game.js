// «Барашек-прыгун» — маленькая оригинальная игра: тап/пробел — прыжок.
(() => {
  const cv = document.getElementById("game");
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const W = 600, H = 200, GROUND = 168;
  let dpr = 1;
  const fit = () => {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = W * dpr; cv.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  fit();

  let best = 0;
  try { best = +localStorage.getItem("sheep-best") || 0; } catch (e) {}

  const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;
  let state = "idle", score = 0, speed = 5, t = 0, last = 0, spawnIn = 0;
  const sheep = { x: 60, y: GROUND, vy: 0, jumps: 0 };
  let obs = [], clouds = [], hills = [];
  for (let i = 0; i < 4; i++) clouds.push({ x: Math.random() * W, y: 20 + Math.random() * 50, s: 0.6 + Math.random() * 0.6 });
  for (let i = 0; i < 5; i++) hills.push({ x: i * 160, h: 40 + Math.random() * 50 });

  const reset = () => {
    state = "run"; score = 0; speed = 5; obs = []; spawnIn = 60;
    sheep.y = GROUND; sheep.vy = 0; sheep.jumps = 0;
  };
  const jump = () => {
    if (state !== "run") { reset(); return; }
    if (sheep.jumps < 2) {                 // двойной прыжок — чтобы было веселее
      sheep.vy = sheep.jumps ? -9.5 : -11.5;
      sheep.jumps++;
    }
  };

  cv.addEventListener("pointerdown", (e) => { e.preventDefault(); jump(); });
  window.addEventListener("keydown", (e) => {
    if ((e.code === "Space" || e.code === "ArrowUp") && document.activeElement === cv) { e.preventDefault(); jump(); }
  });

  const spawn = () => {
    const kind = Math.random() < 0.55 ? "rock" : Math.random() < 0.6 ? "bush" : "pair";
    if (kind === "pair") {
      obs.push({ x: W + 10, w: 22, h: 22, kind: "rock" }, { x: W + 34, w: 22, h: 28, kind: "rock" });
    } else {
      obs.push({ x: W + 10, w: kind === "rock" ? 26 : 30, h: kind === "rock" ? 24 : 34, kind });
    }
    spawnIn = Math.max(38, 95 - speed * 4) + Math.random() * 60;
  };

  const hit = (o) => {
    const sx = sheep.x - 16, sy = sheep.y - 26, sw = 32, sh = 24;   // чуть меньше картинки — честнее
    return sx < o.x + o.w - 4 && sx + sw > o.x + 4 && sy + sh > GROUND - o.h + 4;
  };

  const drawSheep = () => {
    const d = dark(), x = sheep.x, y = sheep.y;
    const leg = state === "run" && sheep.y >= GROUND ? Math.sin(t / 3) * 4 : 0;
    ctx.fillStyle = d ? "#2b2b2b" : "#3a3a3a";
    ctx.fillRect(x - 10 + leg, y - 8, 4, 8); ctx.fillRect(x + 6 - leg, y - 8, 4, 8);
    ctx.fillStyle = d ? "#e9e4da" : "#ffffff";
    ctx.strokeStyle = d ? "#bdb6a8" : "#cfc8bb"; ctx.lineWidth = 1.5;
    [[-10, -18, 9], [0, -22, 10], [10, -18, 9], [-4, -12, 9], [6, -12, 9]].forEach(([dx, dy, r]) => {
      ctx.beginPath(); ctx.arc(x + dx, y + dy, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    });
    ctx.fillStyle = d ? "#2b2b2b" : "#3a3a3a";
    ctx.beginPath(); ctx.ellipse(x + 20, y - 20, 7, 6, 0.2, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(x + 22, y - 22, 1.6, 0, Math.PI * 2); ctx.fill();
  };

  const draw = () => {
    const d = dark();
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, d ? "#16213a" : "#bfe3ff"); g.addColorStop(1, d ? "#2a3550" : "#f3efe6");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = d ? "rgba(255,255,255,.15)" : "rgba(255,255,255,.9)";
    clouds.forEach((c) => { ctx.beginPath(); ctx.ellipse(c.x, c.y, 28 * c.s, 10 * c.s, 0, 0, Math.PI * 2); ctx.fill(); });
    ctx.fillStyle = d ? "#33405e" : "#c9d8c0";
    hills.forEach((h) => { ctx.beginPath(); ctx.moveTo(h.x - 90, GROUND); ctx.lineTo(h.x, GROUND - h.h); ctx.lineTo(h.x + 90, GROUND); ctx.fill(); });
    ctx.fillStyle = d ? "#3c5a3a" : "#8cbf6a"; ctx.fillRect(0, GROUND, W, H - GROUND);
    ctx.fillStyle = d ? "#2f472e" : "#76a956";
    for (let i = 0; i < W; i += 24) ctx.fillRect((i - (t * speed) % 24 + 24) % W, GROUND + 6, 10, 2);
    obs.forEach((o) => {
      if (o.kind === "rock") {
        ctx.fillStyle = d ? "#8a8a96" : "#9c968c";
        ctx.beginPath(); ctx.ellipse(o.x + o.w / 2, GROUND - o.h / 2, o.w / 2, o.h / 2, 0, 0, Math.PI * 2); ctx.fill();
      } else {
        ctx.fillStyle = d ? "#3f7a3a" : "#4f9a3f";
        [[0.3, 0.55], [0.7, 0.55], [0.5, 0.3]].forEach(([fx, fy]) => {
          ctx.beginPath(); ctx.arc(o.x + o.w * fx, GROUND - o.h * (1 - fy), o.w * 0.33, 0, Math.PI * 2); ctx.fill();
        });
      }
    });
    drawSheep();
    ctx.fillStyle = d ? "#e8e2d6" : "#3a3328";
    ctx.font = "bold 16px system-ui, sans-serif"; ctx.textAlign = "right";
    ctx.fillText(String(Math.floor(score)).padStart(5, "0") + "   рекорд " + String(best).padStart(5, "0"), W - 12, 24);
    if (state !== "run") {
      ctx.textAlign = "center"; ctx.font = "bold 20px system-ui, sans-serif";
      ctx.fillText(state === "over" ? "Ой! Нажмите, чтобы ещё раз 🐑" : "Нажмите, чтобы начать 🐑", W / 2, 90);
      ctx.font = "14px system-ui, sans-serif";
      ctx.fillText("тап или пробел — прыжок, можно двойной", W / 2, 115);
    }
  };

  const step = () => {
    t++;
    clouds.forEach((c) => { c.x -= 0.3 * c.s; if (c.x < -40) c.x = W + 40; });
    if (state === "run") {
      hills.forEach((h) => { h.x -= speed * 0.25; if (h.x < -100) h.x += 800; });
      sheep.vy += 0.6; sheep.y += sheep.vy;
      if (sheep.y >= GROUND) { sheep.y = GROUND; sheep.vy = 0; sheep.jumps = 0; }
      obs.forEach((o) => { o.x -= speed; });
      obs = obs.filter((o) => o.x > -50);
      if (--spawnIn <= 0) spawn();
      score += speed / 12;
      speed = Math.min(13, 5 + score / 120);
      if (obs.some(hit)) {
        state = "over";
        if (Math.floor(score) > best) {
          best = Math.floor(score);
          try { localStorage.setItem("sheep-best", String(best)); } catch (e) {}
        }
      }
    }
  };

  const loop = (ts) => {
    if (!last) last = ts;
    let n = 0;
    while (ts - last >= 1000 / 60 && n++ < 5) { step(); last += 1000 / 60; }   // одинаковая скорость на любых экранах
    if (n >= 5) last = ts;
    draw();
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
})();
