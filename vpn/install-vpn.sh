#!/usr/bin/env bash
# Личный VPN на этом сервере: Xray VLESS + Reality (маскируется под обычный https-сайт,
# поэтому его плохо видят российские блокировки). Порт 443 занят Caddy контент-фабрики,
# поэтому VPN слушает 8443 и Caddy/бота не трогает.
#
# Запуск (на сервере, от root):
#   bash install-vpn.sh          поставить (или, если уже стоит, показать ключ)
#   bash install-vpn.sh show     показать ключ и QR-код ещё раз
#   bash install-vpn.sh add Имя  выдать отдельный ключ ещё одному человеку
set -euo pipefail

PORT="${VPN_PORT:-8443}"
SNI="${VPN_SNI:-www.microsoft.com}"
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
    *) die "неизвестная команда: $1 (есть show и add)" ;;
  esac
fi

say "Ставлю нужные программы"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl unzip qrencode python3 >/dev/null

say "Ставлю Xray (официальный установщик)"
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null
command -v xray >/dev/null || die "Xray не установился"

if ss -ltn "sport = :$PORT" | grep -q LISTEN; then
  die "порт $PORT уже занят другой программой. Запустите так: VPN_PORT=9443 bash install-vpn.sh"
fi

say "Создаю ключи"
UUID=$(xray uuid)
KEYS=$(xray x25519)
# Новые версии Xray пишут «PrivateKey/Password», старые «Private key/Public key».
PRIV=$(echo "$KEYS" | awk -F': *' 'tolower($1) ~ /private/ {print $2; exit}')
PUB=$(echo "$KEYS" | awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}')
[ -n "$PRIV" ] && [ -n "$PUB" ] || die "не получилось создать ключи: $KEYS"
SID=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
IP=$(server_ip)

mkdir -p "$DIR"
cat > "$CFG" <<EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": $PORT,
      "protocol": "vless",
      "settings": {
        "clients": [ { "id": "$UUID", "flow": "xtls-rprx-vision", "email": "Alexander" } ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "dest": "$SNI:443",
          "serverNames": [ "$SNI" ],
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
umask 077
cat > "$STATE" <<EOF
IP=$IP
PORT=$PORT
SNI=$SNI
PUB=$PUB
SID=$SID
EOF

xray run -test -config "$CFG" >/dev/null || die "Xray не принял настройки"

say "Включаю ускорение сети (BBR)"
cat > /etc/sysctl.d/99-vpn-speed.conf <<EOF
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
sysctl -q --system || true

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  say "Открываю порт $PORT в файрволе"
  ufw allow "$PORT/tcp" >/dev/null
fi

say "Запускаю VPN"
systemctl enable -q xray
systemctl restart xray
sleep 2
systemctl is-active -q xray || { journalctl -u xray -n 20 --no-pager; die "VPN не запустился"; }

trap - EXIT
print_key "$UUID" "Alexander"
say "Готово. Сайт и бот контент-фабрики не тронуты (они на порту 443)."
