#!/bin/bash
# Будильник для Render: не даёт чату заснуть. Запускается cron раз в минуту и пингует 2 раза (сейчас и через 30 с).
# В Telegram пишет только при смене состояния: чат упал / снова работает.
URL="https://wifi-portal-70yp.onrender.com/healthz"
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV="$DIR/chat/.env"
STATE="$DIR/.keepalive_state"
LOG="$HOME/Logs/wifi_portal/keepalive.log"
mkdir -p "$(dirname "$LOG")"; touch "$LOG"

get() { grep -E "^$1=" "$ENV" | cut -d= -f2-; }
notify() {
  curl -s -m 15 -o /dev/null "https://api.telegram.org/bot$(get TG_BOT_TOKEN)/sendMessage" \
    --data-urlencode "chat_id=$(get TG_CHAT_ID)" --data-urlencode "text=$1"
}

ping_once() {
  code=$(curl -s -m 60 -o /dev/null -w '%{http_code}' "$URL")
  prev=$(cat "$STATE" 2>/dev/null || echo up)
  if [ "$code" = 200 ]; then
    [ "$prev" = down ] && { notify "✅ Wi-Fi чат снова работает"; echo "$(date '+%F %T') UP again" >> "$LOG"; }
    echo up > "$STATE"
  else
    fails=$(( $(cat "$STATE.fails" 2>/dev/null || echo 0) + 1 ))
    echo "$fails" > "$STATE.fails"
    echo "$(date '+%F %T') FAIL code=$code (#$fails)" >> "$LOG"
    # 3 неудачи подряд (~1.5 мин) — считаем, что упал
    if [ "$fails" -ge 3 ] && [ "$prev" = up ]; then
      notify "⚠️ Wi-Fi чат не отвечает (код $code). Гости не видят страницу."
      echo down > "$STATE"
    fi
    return
  fi
  rm -f "$STATE.fails"
}

ping_once
sleep 30
ping_once
# держим лог небольшим
[ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 2000 ] && tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit 0
