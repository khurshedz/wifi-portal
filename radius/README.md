# RADIUS-посредник

Keenetic (Chilli) спрашивает здесь «пускать ли гостя», посредник спрашивает портал (`/api/radius`).
Решение (аккаунт, время, MAC, лимит скорости) принимает портал. Здесь только протокол RADIUS.

Установка на ПК (адрес ПК должен совпадать с `chilli radius` на роутере, сейчас 192.168.4.89):

    mkdir -p ~/.config/systemd/user
    cp assalom-radius.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now assalom-radius
    journalctl --user -u assalom-radius -f      # журнал

Чтобы служба работала без входа в систему: `sudo loginctl enable-linger $USER`.
Настройки берутся из `router_backup/render_env.txt` (PORTAL_URL, UAM_SECRET). Общий ключ с роутером
подбирается автоматически, а если на роутере он задан — укажите его в RADIUS_SECRET.
На VPS всё то же самое, только в `RADIUS_CLIENTS` указывается внешний IP роутера.
