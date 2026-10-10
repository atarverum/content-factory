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
  say "Ключ «$2» (скопируйте целиком одной строкой)"
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
	reverse_proxy 127.0.0.1:8080
}
EOF
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
      python3 - "$CFG" <<'PY' | while read -r id name; do print_key "$id" "$name"; done
import json, sys
for c in json.load(open(sys.argv[1]))["inbounds"][0]["settings"]["clients"]:
    print(c["id"], c.get("email", "VPN"))
PY
      exit 0 ;;
    add)
      NAME="${2:-}"; [ -n "$NAME" ] || die "укажите имя: bash install-vpn.sh add Полина"
      NAME="${NAME// /_}"
      NEW=$(xray uuid)
      python3 - "$CFG" "$NEW" "$NAME" <<'PY'
import json, sys
p, uid, name = sys.argv[1:]
cfg = json.load(open(p))
cfg["inbounds"][0]["settings"]["clients"].append({"id": uid, "flow": "xtls-rprx-vision", "email": name})
json.dump(cfg, open(p, "w"), indent=2)
PY
      systemctl restart xray
      print_key "$NEW" "$NAME"
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
    *) die "неизвестная команда: $1 (есть show, add, move443 и sni)" ;;
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
cat > /etc/sysctl.d/99-vpn-speed.conf <<EOF
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
sysctl -q --system || true

systemctl enable -q xray
move_to_443
trap - EXIT
print_key "$UUID" "Alexander"
say "Готово. Сайт и бот контент-фабрики работают как раньше."
