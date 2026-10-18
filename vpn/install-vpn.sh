#!/usr/bin/env bash
# Личный VPN на этом сервере: Xray VLESS + Reality (маскируется под обычный https-сайт,
# поэтому его плохо видят российские блокировки).
#
# VPN стоит на порту 443: нестандартные порты российские провайдеры режут. Сайт контент-фабрики
# при этом работает как раньше: всех, кто пришёл без ключа VPN, Xray незаметно передаёт в Caddy,
# который теперь слушает только 127.0.0.1:8444 (Reality «маскируется под самого себя»).
#
# Запуск (на сервере, от root):
#   bash install-vpn.sh          поставить (или, если уже стоит, показать ключ)
#   bash install-vpn.sh show     показать ключ и QR-код ещё раз
#   bash install-vpn.sh add Имя  выдать отдельный ключ ещё одному человеку
#   bash install-vpn.sh move443  перенести старую установку с порта 8443 на 443
#   bash install-vpn.sh xhttp    добавить второй вход VLESS через XHTTP на порту 8443
#   bash install-vpn.sh all      поставить всё сразу (XHTTP + Hysteria2) и показать все ключи
#   bash install-vpn.sh auto     подписка для Happ с автопереключением (XHTTP на 443 и 8443)
#   bash install-vpn.sh hy2      добавить запасной протокол Hysteria2 (UDP 443)
#   bash install-vpn.sh clash    файл настроек для Clash Verge Rev
#   bash install-vpn.sh stats    кто сколько накачал, кто онлайн, куда ходили за час
#   bash install-vpn.sh speed    скорость интернета у самого сервера
#   bash install-vpn.sh addnode FI  подключить второй сервер к подписке (см. vpn/node.sh)
#   bash install-vpn.sh delnode FI  убрать второй сервер из подписки
#   bash install-vpn.sh sni САЙТ сменить сайт маскировки (self — сайт фабрики)
set -euo pipefail

CADDY_PORT=8444
DIR=/usr/local/etc/xray
CFG=$DIR/config.json
STATE=$DIR/vpn.env

say() { printf '\n\033[1;35m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "запустите от root"

server_ip() {
  curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}'
}

print_key() {  # $1 = uuid, $2 = подпись
  # shellcheck disable=SC1090
  . "$STATE"
  local link="vless://$1@$IP:$PORT?encryption=none&flow=xtls-rprx-vision&security=reality&sni=$SNI&fp=chrome&pbk=$PUB&sid=$SID&type=tcp#$2"
  case "${3:-}" in
    xhttp) link="vless://$1@$IP:8443?encryption=none&security=reality&sni=$SNI&fp=chrome&pbk=$PUB&sid=$SID&type=xhttp&path=%2F$XPATH&mode=auto#$2-XHTTP" ;;
    xhttp443) link="vless://$1@$IP:443?encryption=none&security=reality&sni=$SNI&fp=chrome&pbk=$PUB&sid=$SID&type=xhttp&path=%2F$XPATH&mode=auto#$2-XHTTP-443" ;;
  esac
  say "Ключ «$2${3:+ ($3)}» (скопируйте целиком одной строкой)"
  echo "$link"
  echo
  qrencode -t ansiutf8 -m 1 "$link"
  echo "Этот QR-код можно отсканировать приложением на iPhone."
}

# Caddyfile, при котором 443 отдан VPN, а сайт живёт за ним на 127.0.0.1:$CADDY_PORT.
write_caddyfile() {  # $1 = домен
  cat > /etc/caddy/Caddyfile <<EOF
# Порт 443 занят VPN (Xray). Посетителей сайта Xray передаёт сюда, на 127.0.0.1:$CADDY_PORT.
{
	https_port $CADDY_PORT
	auto_https disable_redirects
}
http://$1 {
	redir https://$1{uri} permanent
}
$1:$CADDY_PORT {
	bind 127.0.0.1
	# Подписки VPN для Happ (файлы с секретными именами, см. команду auto).
	handle_path /sub/* {
		# Happ сам обновляет подписку раз в час.
		header Content-Type "application/json; charset=utf-8"
		header profile-update-interval "1"
		header profile-title "base64:4ZqxIFJhaWRv"
		root * /etc/xray-sub
		file_server
	}
	handle {
		reverse_proxy 127.0.0.1:8080
	}
}
EOF
}

# Сеть сервера: BBR, большие буферы для дальнего канала Россия — Нидерланды и без «остывания»
# соединения после паузы (иначе каждая новая страница сначала грузится медленно).
tune_net() {
  cat > /etc/sysctl.d/99-vpn-speed.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.ipv4.tcp_slow_start_after_idle=0
net.ipv4.tcp_notsent_lowat=16384
net.ipv4.tcp_mtu_probing=1
net.ipv4.tcp_fastopen=3
net.core.rmem_max=33554432
net.core.wmem_max=33554432
net.ipv4.tcp_rmem=4096 131072 33554432
net.ipv4.tcp_wmem=4096 65536 33554432
EOF
  sysctl -q --system >/dev/null 2>&1 || true
}

# Переводит VPN на 443, а Caddy прячет за ним. Если сайт после этого не открывается,
# всё возвращается как было.
move_to_443() {
  # shellcheck disable=SC1090
  . "$STATE"
  local domain="${IP//./-}.sslip.io" code
  say "Переношу VPN на порт 443 (сайт $domain продолжит работать)"
  cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-vpn
  cp "$CFG" "$CFG.before-443"
  cp "$STATE" "$STATE.before-443"

  write_caddyfile "$domain"
  python3 - "$CFG" "$domain" "$CADDY_PORT" <<'PY'
import json, sys
p, domain, cport = sys.argv[1:]
cfg = json.load(open(p))
ib = cfg["inbounds"][0]
ib["port"] = 443
r = ib["streamSettings"]["realitySettings"]
r["dest"] = "127.0.0.1:" + cport
r["serverNames"] = [domain]
json.dump(cfg, open(p, "w"), indent=2)
PY
  sed -i -e 's/^PORT=.*/PORT=443/' -e "s/^SNI=.*/SNI=$domain/" "$STATE"

  code=000
  if xray run -test -config "$CFG" >/dev/null && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    systemctl restart caddy; sleep 1
    systemctl restart xray; sleep 3
    # Любой ответ сайта (даже 404) значит, что цепочка «Xray → Caddy» и сертификат в порядке.
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$domain/" || true)
  fi
  if [ "$code" != 000 ] && systemctl is-active -q xray && systemctl is-active -q caddy; then
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 443/tcp >/dev/null; fi
    return 0
  fi

  echo "Что-то пошло не так, возвращаю как было. Журнал:"
  journalctl -u xray -u caddy -n 20 --no-pager || true
  cp /etc/caddy/Caddyfile.before-vpn /etc/caddy/Caddyfile
  cp "$CFG.before-443" "$CFG"
  cp "$STATE.before-443" "$STATE"
  systemctl stop xray || true
  systemctl restart caddy || true
  systemctl start xray || true
  die "перенос на 443 не удался, всё вернул как было. Пришлите скриншот этого окна."
}

if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  . "$STATE"
  case "${1:-show}" in
    show)
      python3 - "$CFG" <<'PY' | while read -r id name kind; do print_key "$id" "$name" "$kind"; done
import json, sys
for ib in json.load(open(sys.argv[1]))["inbounds"]:
    if ib.get("protocol") != "vless":
        continue
    kind = "xhttp" if ib.get("tag") == "xhttp" else ("xhttp443" if ib["streamSettings"]["network"] == "xhttp" else "")
    for c in ib["settings"]["clients"]:
        print(c["id"], c.get("email", "VPN"), kind)
PY
      if [ -f /etc/hysteria/vpn.env ]; then exec bash "$0" hy2; fi
      exit 0 ;;
    add)
      NAME="${2:-}"; [ -n "$NAME" ] || die "укажите имя: bash install-vpn.sh add Полина"
      NAME="${NAME// /_}"
      NEW=$(xray uuid)
      python3 - "$CFG" "$NEW" "$NAME" <<'PY'
import json, sys
p, uid, name = sys.argv[1:]
cfg = json.load(open(p))
for ib in cfg["inbounds"]:
    if ib.get("protocol") != "vless":
        continue
    flow = "" if ib["streamSettings"]["network"] == "xhttp" else "xtls-rprx-vision"
    ib["settings"]["clients"].append({"id": uid, "flow": flow, "email": name})
json.dump(cfg, open(p, "w"), indent=2)
PY
      systemctl restart xray
      print_key "$NEW" "$NAME"
      if grep -q '"tag": "xhttp"' "$CFG"; then print_key "$NEW" "$NAME" xhttp; fi
      if [ -d /etc/xray-sub ]; then echo; echo "Подписку с автопереключением для $NAME покажет: bash install-vpn.sh auto"; fi
      exit 0 ;;
    move443)
      if [ "$PORT" = 443 ]; then echo "VPN уже на порту 443."; exit 0; fi
      move_to_443
      say "Готово. Старые ключи больше не работают, вот новые:"
      exec bash "$0" show ;;
    sni)
      # Под какой сайт маскируется VPN. «self» = под сайт фабрики (по умолчанию, сайт работает).
      # Чужой сайт (например www.microsoft.com): провайдеру трафик виден как заход на него,
      # но сайт фабрики на это время не открывается, потому что посетителей Xray отправляет туда.
      [ "$PORT" = 443 ] || die "сначала: bash install-vpn.sh move443"
      TARGET="${2:-}"; [ -n "$TARGET" ] || die "укажите сайт: bash install-vpn.sh sni www.microsoft.com (или self)"
      DOMAIN="${IP//./-}.sslip.io"
      if [ "$TARGET" = self ]; then TARGET=$DOMAIN; DEST="127.0.0.1:$CADDY_PORT"; else DEST="$TARGET:443"; fi
      if [ "$TARGET" != "$DOMAIN" ]; then
        curl -sI -m 10 "https://$TARGET" >/dev/null || die "сервер не может открыть https://$TARGET, выберите другой сайт"
      fi
      python3 - "$CFG" "$TARGET" "$DEST" <<'PY'
import json, sys
p, sni, dest = sys.argv[1:]
cfg = json.load(open(p))
r = cfg["inbounds"][0]["streamSettings"]["realitySettings"]
r["dest"] = dest
r["serverNames"] = [sni]
json.dump(cfg, open(p, "w"), indent=2)
PY
      sed -i "s/^SNI=.*/SNI=$TARGET/" "$STATE"
      xray run -test -config "$CFG" >/dev/null || die "Xray не принял настройки"
      systemctl restart xray
      say "VPN теперь маскируется под $TARGET"
      echo "В Happ откройте сервер и в поле «Имя сервера» впишите: $TARGET"
      [ "$TARGET" = "$DOMAIN" ] || echo "Сайт фабрики сейчас не открывается. Вернуть: bash install-vpn.sh sni self"
      exit 0 ;;
    xhttp)
      # Второй вход VLESS: XHTTP + Reality на TCP 8443 (те же ключи и маскировка, что у основного на 443).
      # Российские фильтры такую связку ловят хуже, чем «tcp + Vision».
      XPATH="${XPATH:-$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
      python3 - "$CFG" "$XPATH" <<'PY'
import json, sys, copy
p, path = sys.argv[1:]
cfg = json.load(open(p))
cfg["inbounds"] = [ib for ib in cfg["inbounds"] if ib.get("tag") != "xhttp"]
main = cfg["inbounds"][0]
ib = copy.deepcopy(main)
ib["tag"] = "xhttp"
ib["port"] = 8443
ib["streamSettings"]["network"] = "xhttp"
ib["streamSettings"]["xhttpSettings"] = {"path": "/" + path, "mode": "auto"}
for c in ib["settings"]["clients"]:
    c["flow"] = ""
cfg["inbounds"].append(ib)
json.dump(cfg, open(p, "w"), indent=2)
PY
      sed -i '/^XPATH=/d' "$STATE"; echo "XPATH=$XPATH" >> "$STATE"
      xray run -test -config "$CFG" >/dev/null || die "Xray не принял настройки"
      if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 8443/tcp >/dev/null; fi
      systemctl restart xray
      exec bash "$0" show ;;
    hy2)
      # Второй, запасной протокол: Hysteria2 по UDP 443. TCP 443 у Xray, UDP 443 свободен
      # (Caddy слушает только 127.0.0.1:8444). Сертификат самоподписанный, клиент сверяет его отпечаток.
      HY=/etc/hysteria
      if [ ! -f "$HY/vpn.env" ]; then
        say "Ставлю Hysteria2 (официальный установщик)"
        bash <(curl -fsSL https://get.hy2.sh/) >/dev/null
        command -v hysteria >/dev/null || die "Hysteria2 не установилась"
        mkdir -p "$HY"
        openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -days 3650 \
          -keyout "$HY/key.pem" -out "$HY/cert.pem" -subj "/CN=${IP//./-}.sslip.io" 2>/dev/null
        chown hysteria "$HY/key.pem" "$HY/cert.pem" 2>/dev/null || chmod 644 "$HY/key.pem"
        HYPASS=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
        cat > "$HY/config.yaml" <<EOF
listen: :443
tls:
  cert: $HY/cert.pem
  key: $HY/key.pem
auth:
  type: password
  password: $HYPASS
masquerade:
  type: proxy
  proxy:
    url: https://${IP//./-}.sslip.io/
    rewriteHost: true
EOF
        PIN=$(openssl x509 -in "$HY/cert.pem" -noout -fingerprint -sha256 | cut -d= -f2)
        (umask 077; printf 'HYPASS=%s\nPIN=%s\n' "$HYPASS" "$PIN" > "$HY/vpn.env")
        if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 443/udp >/dev/null; fi
        systemctl enable -q hysteria-server
        systemctl restart hysteria-server
        sleep 2
        systemctl is-active -q hysteria-server || { journalctl -u hysteria-server -n 20 --no-pager; die "Hysteria2 не запустилась"; }
      fi
      # shellcheck disable=SC1090
      . "$HY/vpn.env"
      LINK="hysteria2://$HYPASS@$IP:443/?sni=${IP//./-}.sslip.io&insecure=1&pinSHA256=$PIN#Alexander-Hysteria2"
      say "Ключ Hysteria2 (скопируйте целиком одной строкой)"
      echo "$LINK"
      echo
      qrencode -t ansiutf8 -m 1 "$LINK"
      exit 0 ;;
    clash)
      # Файл настроек для Clash Verge Rev (ядро mihomo): VLESS + Reality и, если стоит, Hysteria2.
      OUT=/root/clash-vpn.yaml
      UUID=$(python3 -c "import json;print(json.load(open('$CFG'))['inbounds'][0]['settings']['clients'][0]['id'])")
      {
        echo "mixed-port: 7890"
        echo "allow-lan: false"
        echo "mode: rule"
        echo "log-level: warning"
        echo "proxies:"
        echo "  - {name: VLESS, type: vless, server: $IP, port: $PORT, uuid: $UUID, network: tcp, tls: true, udp: true, flow: xtls-rprx-vision, servername: $SNI, client-fingerprint: chrome, reality-opts: {public-key: $PUB, short-id: $SID}}"
        HYNAME=""
        if [ -f /etc/hysteria/vpn.env ]; then
          # shellcheck disable=SC1091
          . /etc/hysteria/vpn.env
          echo "  - {name: Hysteria2, type: hysteria2, server: $IP, port: 443, password: $HYPASS, sni: ${IP//./-}.sslip.io, skip-cert-verify: true}"
          HYNAME=", Hysteria2"
        fi
        echo "proxy-groups:"
        echo "  - {name: VPN, type: select, proxies: [VLESS$HYNAME]}"
        echo "rules:"
        echo "  - MATCH,VPN"
      } > "$OUT"
      chmod 600 "$OUT"
      say "Файл для Clash Verge Rev готов: $OUT"
      echo "Скачайте его на Mac командой (на Mac): scp root@$IP:$OUT ~/Downloads/"
      exit 0 ;;
    auto)
      # Подписка для Happ (JSON): вход XHTTP 8443, русские сайты напрямую. Обновляется в Happ сама,
      # так что новые входы (например, мост в России) появятся у всех без переустановки.
      [ "$PORT" = 443 ] || die "сначала: bash install-vpn.sh move443"
      grep -q '"tag": "xhttp"' "$CFG" || bash "$0" xhttp >/dev/null
      # shellcheck disable=SC1090
      . "$STATE"
      # Основной вход на 443 тоже переводим на XHTTP: «tcp + Vision» российские провайдеры режут.
      python3 - "$CFG" "$XPATH" <<'PY'
import json, sys
p, path = sys.argv[1:]
cfg = json.load(open(p))
ib = cfg["inbounds"][0]
ib["streamSettings"]["network"] = "xhttp"
ib["streamSettings"]["xhttpSettings"] = {"path": "/" + path, "mode": "auto"}
for c in ib["settings"]["clients"]:
    c["flow"] = ""
json.dump(cfg, open(p, "w"), indent=2)
PY
      xray run -test -config "$CFG" >/dev/null || die "Xray не принял настройки"
      DOMAIN="${IP//./-}.sslip.io"
      mkdir -p /etc/xray-sub
      python3 - "$CFG" "$IP" "$SNI" "$PUB" "$SID" "$XPATH" "$DIR/nodes" <<'PY' > /etc/xray-sub/.list
import json, sys, hashlib
import glob, os
p, ip, sni, pub, sid, path, nodes_dir = sys.argv[1:]
cfg = json.load(open(p))
priv = cfg["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"]
# Серверы подписки: главный (Нидерланды) и подключённые командой addnode (имя файла = код страны).
FLAGS = {"FI": "🇫🇮", "RU": "🇷🇺", "DE": "🇩🇪", "SE": "🇸🇪", "EE": "🇪🇪", "LV": "🇱🇻", "NL": "🇳🇱"}
servers = [dict(code="NL", ip=ip, port=8443, sni=sni, pub=pub, sid=sid, path=path)]
for f in sorted(glob.glob(os.path.join(nodes_dir, "*.json"))):
    servers.append(dict(json.load(open(f)), code=os.path.basename(f)[:-5]))
# routeOnly выключен: сервер сам узнаёт адрес сайта (DNS в Нидерландах, без утечек).
sniff = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False}
# Российские зоны и сервисы идут мимо VPN. Список, а не geosite/geoip: на iPhone у VPN всего
# 50 МБ памяти, полные базы стран туда не влезают (VPN падает и долго стартует).
RU_ZONES = ["ru", "su", "xn--p1ai", "moscow", "xn--p1acf", "xn--80asehdb", "xn--c1avg", "xn--80aswg",
            "xn--80adxhks", "xn--d1acj3b"]
RU_SITES = """
2gis.com 2gis.ru aeroflot.ru alfabank.ru alfabank.st auto.ru aviasales.ru avito.ru avito.st beeline.ru
best2pay.net boxberry.ru cbr.ru cdek.ru cian.ru citilink.ru dns-shop.ru drom.ru dzen.ru gazprombank.ru
gismeteo.ru gosuslugi.ru gov.ru hh.ru ivi.ru kinopoisk.ru lamoda.ru mail.ru max.ru megafon.ru megamarket.ru
mironline.ru mkb.ru mos.ru mts.ru mvideo.ru mycdn.me nspk.ru ok.ru okko.tv oneme.ru open.ru ozon.ru ozone.ru
pobeda.aero pochta.ru pochtabank.ru premier.one psbank.ru raiffeisen.ru rostelecom.ru rshb.ru rt.ru rutube.ru
rzd.ru s7.ru sber.ru sberbank.ru sbrf.ru smotrim.ru sovcombank.ru tbank.ru tele2.ru tinkoff.ru userapi.com
vk-cdn.net vk.com vk.me vk.ru vkuser.net vkuservideo.net vtb.ru wb.ru wbbasket.ru wildberries.ru wink.ru
ya.ru yandex.com yandex.net yandex.ru yandexcloud.net yastatic.net yoomoney.ru yota.ru okcdn.ru
ip.mail.ru ipv4-internet.yandex.net ipv6-internet.yandex.net
apple.com icloud.com icloud-content.com mzstatic.com cdn-apple.com aaplimg.com apple-dns.net itunes.com
me.com mac.com apple-cloudkit.com apple-mapkit.com appleid.com
microsoft.com windows.com windowsupdate.com live.com office.com office.net outlook.com skype.com
""".split()
# Через VPN даже если домен попал в список выше (а также Telegram по адресам: приложение ходит без DNS).
PROXY_SITES = ["copilot.microsoft.com", "gemini.gstatic.com"]
TELEGRAM_IP = ["91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22", "91.108.16.0/22", "91.108.20.0/22",
               "91.108.56.0/22", "91.105.192.0/23", "149.154.160.0/20", "185.76.151.0/24",
               "95.161.64.0/20", "2001:67c:4e8::/48", "2001:b28:f23c::/48", "2001:b28:f23d::/48",
               "2001:b28:f23f::/48", "2a0a:f280::/32"]
PRIVATE_IP = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
              "100.64.0.0/10", "fc00::/7", "fe80::/10"]
YANDEX_DNS = ["77.88.8.8", "77.88.8.1"]
ru_domains = ["domain:" + d for d in RU_ZONES + RU_SITES]
for c in cfg["inbounds"][0]["settings"]["clients"]:
    uid, name = c["id"], c.get("email", "VPN")
    token = hashlib.sha256((uid + priv).encode()).hexdigest()[:24]
    def out(srv):
        return {"tag": "vpn-" + srv["code"].lower(), "protocol": "vless",
                "settings": {"vnext": [{"address": srv["ip"], "port": int(srv["port"]),
                    "users": [{"id": uid, "encryption": "none", "flow": ""}]}]},
                "streamSettings": {"network": "xhttp", "security": "reality",
                    # Трафик раскладывается на несколько TCP-соединений и они регулярно обновляются:
                    # если провайдер придушит одно, остальные тянут, а придушенное скоро сменится.
                    "xhttpSettings": {"path": "/" + srv["path"], "mode": "auto",
                        "extra": {"xmux": {"maxConcurrency": "4-8", "cMaxReuseTimes": 0,
                                           "hMaxRequestTimes": "600-900", "hMaxReusableSecs": "600-1200"}}},
                    "realitySettings": {"serverName": srv["sni"], "fingerprint": "chrome",
                        "publicKey": srv["pub"], "shortId": srv["sid"]},
                    "sockopt": {"tcpKeepAliveIdle": 30}}}
    def conf(remarks, chosen):
        outs = [out(x) for x in chosen]
        tags = [o["tag"] for o in outs]
        # Несколько серверов: Happ раз в минуту меряет каждый и идёт через самый быстрый.
        # Пока замеров нет (первые секунды) или все «не ответили», идёт через первый (Нидерланды).
        final = {"balancerTag": "auto"} if len(outs) > 1 else {"outboundTag": tags[0]}
        c = {
            "remarks": remarks,
            # Российские сайты узнаём у Яндекса (так они отдают ближайший сервер), остальное через Google DoH.
            "dns": {"hosts": {"dns.google": ["8.8.8.8", "8.8.4.4"]},
                    "servers": [{"address": YANDEX_DNS[0], "domains": ru_domains, "skipFallback": True},
                                "https://dns.google/dns-query"],
                    "queryStrategy": "UseIPv4"},
            "policy": {"levels": {"0": {"handshake": 4, "connIdle": 300, "uplinkOnly": 1, "downlinkOnly": 1}}},
            "inbounds": [
                {"tag": "socks-in", "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
                 "settings": {"auth": "noauth", "udp": True}, "sniffing": sniff},
                {"tag": "http-in", "listen": "127.0.0.1", "port": 10809, "protocol": "http",
                 "settings": {}, "sniffing": sniff}],
            "outbounds": outs + [
                {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}},
                {"tag": "block", "protocol": "blackhole"}],
            "routing": {"domainStrategy": "AsIs",
                "rules": [
                    {"type": "field", "ip": PRIVATE_IP + YANDEX_DNS + [x["ip"] for x in servers],
                     "outboundTag": "direct"},
                    dict({"type": "field", "domain": ["domain:" + d for d in PROXY_SITES]}, **final),
                    dict({"type": "field", "ip": TELEGRAM_IP}, **final),
                    {"type": "field", "domain": ru_domains, "outboundTag": "direct"},
                    # QUIC (HTTP/3) через туннель тормозит: браузер сразу уходит на обычный HTTPS.
                    {"type": "field", "network": "udp", "port": "443", "outboundTag": "block"},
                    dict({"type": "field", "network": "tcp,udp"}, **final)]}}
        if len(outs) > 1:
            c["observatory"] = {"subjectSelector": ["vpn-"], "probeURL": "https://www.google.com/generate_204",
                                "probeInterval": "1m", "enableConcurrency": True}
            c["routing"]["balancers"] = [{"tag": "auto", "selector": ["vpn-"],
                                          "strategy": {"type": "leastPing"}, "fallbackTag": tags[0]}]
        return c
    name_of = lambda x: f'{FLAGS.get(x["code"], "🌐")} Raido {x["code"]}'
    if len(servers) == 1:
        body = conf(name_of(servers[0]), servers)
    else:
        # Список в Happ: «самый быстрый» сверху, ниже каждый сервер отдельно.
        body = [conf("⚡ Raido Auto", servers)] + [conf(name_of(x), [x]) for x in servers]
    with open("/etc/xray-sub/" + token, "w") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    print(token, name)
PY
      chmod 755 /etc/xray-sub; chmod 644 /etc/xray-sub/*
      cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-sub
      write_caddyfile "$DOMAIN"
      if ! cmp -s /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-sub; then
        caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
          || { cp /etc/caddy/Caddyfile.before-sub /etc/caddy/Caddyfile; die "Caddy не принял настройки, вернул как было"; }
        systemctl restart caddy
      fi
      tune_net
      systemctl restart xray; sleep 2
      systemctl is-active -q xray || die "VPN не запустился"
      while read -r token name; do
        URL="https://$DOMAIN/sub/$token"
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$URL" || true)
        say "Подписка «$name» (проверка: $code). Скопируйте ссылку целиком:"
        echo "$URL"
        echo
        qrencode -t ansiutf8 -m 1 "$URL"
      done < /etc/xray-sub/.list
      exit 0 ;;
    stats)
      # Кто сколько накачал и куда ходил. При первом запуске включает в Xray счётчики и журнал сайтов.
      if ! grep -q '"StatsService"' "$CFG"; then
        cp "$CFG" "$CFG.before-stats"
        python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg["log"] = {"loglevel": "warning", "access": "/var/log/xray/access.log"}
cfg["stats"] = {}
cfg["api"] = {"tag": "api", "services": ["StatsService"]}
cfg["policy"] = {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True, "statsUserOnline": True}},
                 "system": {"statsInboundUplink": True, "statsInboundDownlink": True}}
cfg["inbounds"] = [i for i in cfg["inbounds"] if i.get("tag") != "api"] + [
    {"tag": "api", "listen": "127.0.0.1", "port": 10085, "protocol": "dokodemo-door",
     "settings": {"address": "127.0.0.1"}}]
r = cfg.setdefault("routing", {}).setdefault("rules", [])
if not any(x.get("outboundTag") == "api" for x in r):
    r.insert(0, {"type": "field", "inboundTag": ["api"], "outboundTag": "api"})
json.dump(cfg, open(p, "w"), indent=2)
PY
        mkdir -p /var/log/xray && touch /var/log/xray/access.log && chmod 644 /var/log/xray/access.log
        id nobody >/dev/null 2>&1 && chown nobody /var/log/xray /var/log/xray/access.log 2>/dev/null || true
        # Журнал сайтов храним 3 дня, чтобы не копить чужую историю и не забивать диск.
        cat > /etc/logrotate.d/xray-vpn <<'EOF'
/var/log/xray/access.log {
	daily
	rotate 3
	missingok
	notifempty
	compress
	copytruncate
}
EOF
        if ! xray run -test -config "$CFG" >/dev/null; then
          cp "$CFG.before-stats" "$CFG"; die "Xray не принял настройки, вернул как было"
        fi
        command -v vnstat >/dev/null || apt-get install -y -qq vnstat >/dev/null 2>&1 || true
        systemctl enable -q --now vnstat 2>/dev/null || true
        systemctl restart xray; sleep 2
        systemctl is-active -q xray || { cp "$CFG.before-stats" "$CFG"; systemctl restart xray; die "VPN не запустился, вернул как было"; }
        say "Счётчики включены. Данные копятся с этой минуты."
      fi
      say "Трафик по людям (с последнего перезапуска VPN)"
      xray api statsquery --server=127.0.0.1:10085 2>/dev/null | python3 -c '
import json, sys
try:
    stats = json.load(sys.stdin).get("stat", [])
except Exception:
    stats = []
users = {}
for s in stats:
    parts = s["name"].split(">>>")
    if parts[0] == "user" and len(parts) == 4:
        users.setdefault(parts[1], {})[parts[3]] = int(s.get("value", 0))
if not users:
    print("  пока пусто: никто не подключался")
for name, v in sorted(users.items(), key=lambda kv: -sum(kv[1].values())):
    down, up = v.get("downlink", 0) / 1e6, v.get("uplink", 0) / 1e6
    print(f"  {name:<14} скачал {down:9.1f} МБ   отправил {up:8.1f} МБ")
'
      say "Сейчас онлайн"
      for u in $(python3 -c "import json;c=json.load(open('$CFG'));print(' '.join(sorted({x.get('email','') for i in c['inbounds'] if i.get('protocol')=='vless' for x in i['settings']['clients']})))"); do
        ips=$(xray api statsonlineiplist --server=127.0.0.1:10085 -email "$u" 2>/dev/null | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin).get("ips", {})))
except Exception: print(0)')
        [ "${ips:-0}" -gt 0 ] && echo "  $u: устройств $ips"
      done
      say "Куда ходили за последний час (сайт: сколько раз)"
      python3 - <<'PY'
import re, collections, datetime
cut = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S")
per = collections.defaultdict(collections.Counter)
try:
    lines = open("/var/log/xray/access.log", errors="replace").readlines()
except OSError:
    lines = []
for line in lines:
    if line[:19] < cut or " accepted " not in line:
        continue
    m = re.search(r"accepted (?:tcp|udp):([^:\s]+):\d+.*email: (\S+)", line)
    if not m:
        continue
    host = m.group(1)
    if not re.fullmatch(r"[\d.]+", host):
        host = ".".join(host.split(".")[-2:])
    per[m.group(2)][host] += 1
if not per:
    print("  пока пусто")
for user, c in per.items():
    print(f"  {user}: " + ", ".join(f"{h} {n}" for h, n in c.most_common(10)))
PY
      if command -v vnstat >/dev/null; then
        say "Весь трафик сервера за месяц (VPN + фабрика)"
        vnstat -m 2>/dev/null | tail -n 4 || echo "  статистика появится через несколько минут"
      fi
      exit 0 ;;
    export)
      # Список людей для второго сервера (см. vpn/node.sh). Только идентификаторы ключей и имена.
      python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["inbounds"][0]["settings"]["clients"]; print(json.dumps({"clients":[{"id":x["id"],"email":x.get("email","VPN")} for x in c]}))' "$CFG"
      exit 0 ;;
    addnode)
      # Подключает второй сервер к подписке: параметры приходят из vpn/node.sh по трубе.
      CODE=$(echo "${2:-}" | tr '[:lower:]' '[:upper:]')
      [[ "$CODE" =~ ^[A-Z]{2}$ ]] || die "укажите код страны из двух букв: addnode FI"
      mkdir -p "$DIR/nodes"
      python3 -c '
import json, sys
d = json.load(sys.stdin)
assert all(d.get(k) for k in ("ip", "port", "sni", "pub", "sid", "path"))
json.dump(d, open(sys.argv[1], "w"))' "$DIR/nodes/$CODE.json" || die "не получил параметры второго сервера (вторая часть команды)"
      say "Сервер $CODE добавлен в подписку"
      exec bash "$0" auto ;;
    delnode)
      CODE=$(echo "${2:-}" | tr '[:lower:]' '[:upper:]')
      rm -f "$DIR/nodes/${CODE:?укажите код страны}.json"
      exec bash "$0" auto ;;
    speed)
      # Скорость самого сервера (без VPN и без вашего провайдера): если она высокая, а у вас медленно,
      # дело в пути от вас до сервера.
      say "Скорость интернета у сервера (скачиваю тестовые файлы)"
      for u in https://nbg1-speed.hetzner.com/100MB.bin https://proof.ovh.net/files/100Mb.dat; do
        sp=$(curl -s -o /dev/null --max-time 20 -w '%{speed_download}' "$u" || echo 0)
        python3 -c "import sys; print(f'  {sys.argv[1]:<45} {float(sys.argv[2])*8/1e6:7.0f} Мбит/с')" "${u#https://}" "$sp"
      done
      echo "  Ускорение сети: $(sysctl -n net.ipv4.tcp_congestion_control), без остывания: $(sysctl -n net.ipv4.tcp_slow_start_after_idle | sed 's/0/да/;s/1/нет/')"
      exit 0 ;;
    all)
      bash "$0" sni self >/dev/null
      grep -q '"tag": "xhttp"' "$CFG" || bash "$0" xhttp >/dev/null
      [ -f /etc/hysteria/vpn.env ] || bash "$0" hy2 >/dev/null
      exec bash "$0" show ;;
    *) die "неизвестная команда: $1 (есть show, add, move443, sni, xhttp, hy2, all, auto, stats, speed, export, addnode, delnode и clash)" ;;
  esac
fi

command -v caddy >/dev/null && [ -f /etc/caddy/Caddyfile ] || die "не найден Caddy: сначала поставьте контент-фабрику (install.sh)"

say "Ставлю нужные программы"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl unzip qrencode python3 >/dev/null

say "Ставлю Xray (официальный установщик)"
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null
command -v xray >/dev/null || die "Xray не установился"

say "Создаю ключи"
UUID=$(xray uuid)
KEYS=$(xray x25519)
# Новые версии Xray пишут «PrivateKey/Password», старые «Private key/Public key».
PRIV=$(echo "$KEYS" | awk -F': *' 'tolower($1) ~ /private/ {print $2; exit}')
PUB=$(echo "$KEYS" | awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}')
[ -n "$PRIV" ] && [ -n "$PUB" ] || die "не получилось создать ключи: $KEYS"
SID=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
IP=$(server_ip)

# Порт, адрес маскировки и имя сервера заполнит move_to_443.
mkdir -p "$DIR"
cat > "$CFG" <<EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": 8443,
      "protocol": "vless",
      "settings": {
        "clients": [ { "id": "$UUID", "flow": "xtls-rprx-vision", "email": "Alexander" } ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "dest": "127.0.0.1:$CADDY_PORT",
          "serverNames": [ "localhost" ],
          "privateKey": "$PRIV",
          "shortIds": [ "$SID" ]
        }
      },
      "sniffing": { "enabled": true, "destOverride": [ "http", "tls", "quic" ] }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "direct" },
    { "protocol": "blackhole", "tag": "block" }
  ],
  "routing": {
    "rules": [ { "type": "field", "ip": [ "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10", "::1/128" ], "outboundTag": "block" } ]
  }
}
EOF
chmod 644 "$CFG"
# Если дальше что-то сломается, не оставляем полуготовую установку: повторный запуск начнёт заново.
trap 'rm -f "$STATE"' EXIT
(umask 077; cat > "$STATE" <<EOF
IP=$IP
PORT=8443
SNI=localhost
PUB=$PUB
SID=$SID
EOF
)

say "Включаю ускорение сети (BBR)"
tune_net

systemctl enable -q xray
move_to_443
trap - EXIT
print_key "$UUID" "Alexander"
say "Готово. Сайт и бот контент-фабрики работают как раньше."
