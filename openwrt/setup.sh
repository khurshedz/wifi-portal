#!/bin/sh
# Настройка OpenWrt (Xiaomi Mi Router 3A) под портал «Assalom Wi-Fi».
# Запускать НА РОУТЕРЕ после прошивки OpenWrt и копирования папки files/ (см. README.md).
# Схема: WAN — интернет (кабель от Кинетика/ONT); LAN-порты — админ (192.168.1.1);
#        Wi-Fi «Assalom Wi-Fi» — гости (192.168.2.1), интернета у гостей нет, только наша страница.
set -e
SSID="${SSID:-Assalom Wi-Fi}"

opkg update
opkg install curl ca-bundle

# --- файлы портала
chmod 755 /usr/bin/portal-sync /usr/share/portal/portal.cgi

# --- гостевая сеть
uci -q delete network.guest || true
uci set network.guest=interface
uci set network.guest.proto='static'
uci set network.guest.ipaddr='192.168.2.1'
uci set network.guest.netmask='255.255.255.0'

# --- Wi-Fi: одна открытая сеть для гостей; админ — только по кабелю
uci set wireless.radio0.disabled='0'
uci set wireless.radio0.country='TJ'
uci set wireless.default_radio0.ssid="$SSID"
uci set wireless.default_radio0.encryption='none'
uci set wireless.default_radio0.network='guest'
uci set wireless.default_radio0.isolate='1'        # гости не видят друг друга

# --- DHCP + отдельный DNS для гостей: ЛЮБОЙ адрес -> 192.168.2.1 (телефон сам покажет «Войти в сеть»)
uci set dhcp.@dnsmasq[0].notinterface='guest'
uci -q delete dhcp.guestdns || true
uci set dhcp.guestdns=dnsmasq
uci set dhcp.guestdns.interface='guest'
uci set dhcp.guestdns.localuse='0'
uci set dhcp.guestdns.noresolv='1'
uci add_list dhcp.guestdns.address='/#/192.168.2.1'
uci set dhcp.guestdns.leasefile='/tmp/dhcp.leases.guest'
uci -q delete dhcp.guest || true
uci set dhcp.guest=dhcp
uci set dhcp.guest.interface='guest'
uci set dhcp.guest.instance='guestdns'
uci set dhcp.guest.start='10'
uci set dhcp.guest.limit='200'
uci set dhcp.guest.leasetime='1h'

# --- файрвол: гостям — только DHCP, DNS и страница; в интернет и в админку — нельзя
uci -q delete firewall.guest || true
uci set firewall.guest=zone
uci set firewall.guest.name='guest'
uci set firewall.guest.network='guest'
uci set firewall.guest.input='REJECT'
uci set firewall.guest.output='ACCEPT'
uci set firewall.guest.forward='REJECT'
for r in "guest_dhcp udp 67-68" "guest_dns tcpudp 53" "guest_http tcp 80"; do
    set -- $r
    uci -q delete firewall.$1 || true
    uci set firewall.$1=rule
    uci set firewall.$1.name="$1"
    uci set firewall.$1.src='guest'
    uci set firewall.$1.proto="$( [ "$2" = tcpudp ] && echo 'tcp udp' || echo "$2")"
    uci set firewall.$1.dest_port="$3"
    uci set firewall.$1.target='ACCEPT'
done

# --- веб: LuCI только на LAN, портал — на гостевом адресе
uci -q delete uhttpd.main.listen_http || true
uci -q delete uhttpd.main.listen_https || true
uci add_list uhttpd.main.listen_http='192.168.1.1:80'
uci add_list uhttpd.main.listen_https='192.168.1.1:443'
uci -q delete uhttpd.portal || true
uci set uhttpd.portal=uhttpd
uci add_list uhttpd.portal.listen_http='192.168.2.1:80'
uci set uhttpd.portal.home='/tmp/portal'
uci set uhttpd.portal.cgi_prefix='/cgi-bin'
uci set uhttpd.portal.error_page='/index.html'   # любой путь (generate_204, hotspot-detect…) -> наша страница
uci set uhttpd.portal.no_dirlists='1'
uci set uhttpd.portal.max_requests='20'
uci set uhttpd.portal.script_timeout='30'
uci set uhttpd.portal.network_timeout='30'

# --- расписание: каждые 5 мин — пинг Render + очередь чата; в HH:01 — полная синхронизация
touch /etc/crontabs/root
sed -i '/portal-sync/d' /etc/crontabs/root
echo '*/5 * * * * /usr/bin/portal-sync quick >/dev/null 2>&1' >> /etc/crontabs/root
echo '1 * * * * /usr/bin/portal-sync >/dev/null 2>&1' >> /etc/crontabs/root
grep -q portal-sync /etc/rc.local || sed -i 's|^exit 0|(sleep 40; /usr/bin/portal-sync) \&\nexit 0|' /etc/rc.local

uci commit
/etc/init.d/cron enable
echo "Готово. Перезагрузка через 5 с…"; sleep 5; reboot
