#!/bin/sh
# CGI на роутере: /cgi-bin/portal/api/... и /cgi-bin/portal/media/<id>
# Есть интернет — проксирует чат на Render; нет — отдаёт кеш и копит сообщения в очередь.
SRC="${PORTAL_SRC:-https://wifi-portal-70yp.onrender.com}"
DIR="${PORTAL_DIR:-/tmp/portal}"
STATE="${PORTAL_STATE:-/tmp/portal-state}"
UA="wifi-portal-router/1.0"
P="$PATH_INFO"

# --- сессия: свой cookie sid (как на Render), без Secure — роутер отдаёт по http
SID=$(echo "$HTTP_COOKIE" | tr ';' '\n' | sed -n 's/^ *sid=\([A-Za-z0-9_-]\{40,64\}\) *$/\1/p' | head -n 1)
SETC=""
if [ -z "$SID" ]; then
    SID=$(hexdump -n 24 -e '24/1 "%02x"' /dev/urandom)
    SETC="Set-Cookie: sid=$SID; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax"
fi

reply() {  # reply <status> <content-type> ; тело — из stdin
    echo "Status: $1"
    echo "Content-Type: $2"
    echo "Cache-Control: no-store"
    echo "X-Content-Type-Options: nosniff"
    [ -n "$SETC" ] && echo "$SETC"
    echo
    cat
}
json() { echo "$2" | reply "$1" "application/json"; }

online() { [ -f "$STATE/online" ]; }

proxy() {  # proxy <method> <path?query> [bodyfile] — 0 если сервер ответил
    out=$(mktemp)
    if [ -n "$3" ]; then
        code=$(curl -sS -m 20 -A "$UA" -o "$out" -w '%{http_code}' -X "$1" -H 'Content-Type: application/json' \
               -H 'X-Requested-With: chat' -b "sid=$SID" --data-binary @"$3" "$SRC$2")
    else
        code=$(curl -sS -m 20 -A "$UA" -o "$out" -w '%{http_code}' -H 'X-Requested-With: chat' -b "sid=$SID" "$SRC$2")
    fi
    if [ "${code:-000}" = "000" ]; then rm -f "$out"; rm -f "$STATE/online"; return 1; fi
    reply "$code" "application/json" < "$out"; rm -f "$out"; return 0
}

case "$P" in
/api/info)
    [ -f "$DIR/info.json" ] || { json 503 '{"detail":"нет данных"}'; exit; }
    if online; then off=false; else off=true; fi
    { printf '{"offline":%s,"synced":%s,' "$off" "$(cat "$STATE/synced" 2>/dev/null || echo 0)"
      tail -c +2 "$DIR/info.json"; } | reply 200 "application/json"
    ;;
/api/me)
    online && proxy GET /api/me && exit
    json 200 '{"num":null,"name":null}'
    ;;
/api/messages)
    after=$(echo "$QUERY_STRING" | sed -n 's/.*after=\([0-9]\{1,12\}\).*/\1/p')
    online && proxy GET "/api/messages?after=${after:-0}" && exit
    json 200 '{"messages":[]}'
    ;;
/api/send)
    [ "$REQUEST_METHOD" = "POST" ] && [ "$HTTP_X_REQUESTED_WITH" = "chat" ] || { json 403 '{"detail":"forbidden"}'; exit; }
    len=${CONTENT_LENGTH:-0}
    case "$len" in ''|*[!0-9]*) len=0;; esac
    [ "$len" -gt 0 ] && [ "$len" -le 8192 ] || { json 413 '{"detail":"слишком длинно"}'; exit; }
    body=$(mktemp); head -c "$len" > "$body"
    if online && proxy POST /api/send "$body"; then rm -f "$body"; exit; fi
    mkdir -p "$STATE/queue"
    if [ "$(ls "$STATE/queue" | wc -l)" -ge 500 ]; then rm -f "$body"; json 503 '{"detail":"очередь переполнена"}'; exit; fi
    { echo "$SID"; cat "$body"; } > "$STATE/queue/$(date +%s)-$$"; rm -f "$body"
    json 200 '{"num":null,"queued":true}'
    ;;
/api/room)
    # есть интернет — общий чат на Render (с модерацией); нет — локальный на роутере (RAM)
    if online; then
        if [ "$REQUEST_METHOD" = "POST" ]; then
            [ "$HTTP_X_REQUESTED_WITH" = "chat" ] || { json 403 '{"detail":"forbidden"}'; exit; }
            len=${CONTENT_LENGTH:-0}; case "$len" in ''|*[!0-9]*) len=0;; esac
            [ "$len" -gt 0 ] && [ "$len" -le 4096 ] || { json 413 '{"detail":"слишком длинно"}'; exit; }
            body=$(mktemp); head -c "$len" > "$body"
            proxy POST /api/room "$body" && { rm -f "$body"; exit; }
            # Render не ответил — пишем локально
            exec 0< "$body"; rm -f "$body"; CONTENT_LENGTH=$len
        else
            after=$(echo "$QUERY_STRING" | sed -n 's/.*after=\([0-9]\{1,12\}\).*/\1/p')
            proxy GET "/api/room?after=${after:-0}" && exit
        fi
    fi
    R="$STATE/room"; mkdir -p "$R"; touch "$R/log" "$R/users"
    ME=$(echo "$SID" | md5sum | cut -c1-10)
    if [ "$REQUEST_METHOD" = "POST" ]; then
        [ "$HTTP_X_REQUESTED_WITH" = "chat" ] || { json 403 '{"detail":"forbidden"}'; exit; }
        len=${CONTENT_LENGTH:-0}; case "$len" in ''|*[!0-9]*) len=0;; esac
        [ "$len" -gt 0 ] && [ "$len" -le 4096 ] || { json 413 '{"detail":"слишком длинно"}'; exit; }
        body=$(head -c "$len")
        now=$(date +%s)
        last=$(cat "$R/last-$ME" 2>/dev/null || echo 0)
        [ $((now - last)) -ge 2 ] || { json 429 '{"detail":"Не так быстро 🙂"}'; exit; }
        esc() { tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
        text=$(jsonfilter -s "$body" -e '@.text' 2>/dev/null | head -c 1500 | esc)
        name=$(jsonfilter -s "$body" -e '@.name' 2>/dev/null | head -c 100 | esc)
        [ -n "$(echo "$text" | tr -d ' ')" ] || { json 400 '{"detail":"пустое сообщение"}'; exit; }
        num=$(grep "^$ME " "$R/users" | cut -d' ' -f2)
        if [ -z "$num" ]; then num=$(( $(wc -l < "$R/users") + 1 )); echo "$ME $num" >> "$R/users"; fi
        echo "$now" > "$R/last-$ME"
        (
            flock 9
            id=$(( $(tail -n 1 "$R/log" | sed -n 's/^{"id":\([0-9]*\),.*/\1/p') + 0 + 1 ))
            printf '{"id":%s,"ts":%s,"num":%s,"u":"%s","name":"%s","text":"%s"}\n' "$id" "$now" "$num" "$ME" "$name" "$text" >> "$R/log"
            tail -n 200 "$R/log" > "$R/log.tmp" && mv "$R/log.tmp" "$R/log"
        ) 9>"$R/lock"
        json 200 "{\"ok\":true,\"num\":$num}"
    else
        after=$(echo "$QUERY_STRING" | sed -n 's/.*after=\([0-9]\{1,12\}\).*/\1/p')
        { printf '{"src":"local","you":"%s","deleted":[],"messages":[' "$ME"
          awk -v a="${after:-0}" -F'[:,]' '$2+0 > a+0' "$R/log" | tail -n 100 | sed '$!s/$/,/'
          printf ']}'; } | reply 200 "application/json"
    fi
    ;;
/media/*)
    id=${P#/media/}
    echo "$id" | grep -q '^[0-9a-f]\{16\}$' && [ -f "$DIR/media/$id" ] || { json 404 '{"detail":"not found"}'; exit; }
    ct=$(cat "$DIR/media/$id.type" 2>/dev/null); case "$ct" in image/*) ;; *) ct=image/jpeg;; esac
    echo "Status: 200"; echo "Content-Type: $ct"; echo "Cache-Control: public, max-age=86400"
    case "$QUERY_STRING" in *dl=1*) echo "Content-Disposition: attachment; filename=\"foto-dnya-$(echo "$id" | cut -c1-6).jpg\"";; esac
    echo; cat "$DIR/media/$id"
    ;;
*)
    json 404 '{"detail":"not found"}'
    ;;
esac
