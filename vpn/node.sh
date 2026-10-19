#!/usr/bin/env bash
# Второй сервер VPN Raido (например, в Финляндии). Ставится на чистый Ubuntu 22.04/24.04 и
# подключается к подписке главного сервера. Запуск одной командой с Mac (подставьте адрес и имя):
#
#   ssh root@ГЛАВНЫЙ 'bash install-vpn.sh export' \
#     | ssh root@НОВЫЙ 'curl -fsSL URL/vpn/node.sh -o node.sh && bash node.sh' \
#     | ssh root@ГЛАВНЫЙ 'bash install-vpn.sh addnode FI'
#
# На вход (stdin) получает список людей с главного сервера, на выход (stdout) отдаёт параметры
# этого сервера одной строкой JSON. Всё остальное печатает в stderr, чтобы не мешать трубе.
# Повторный запуск безопасен: ключи сервера сохраняются, список людей обновляется.
set -euo pipefail

DIR=/usr/local/etc/xray
CFG=$DIR/config.json
STATE=$DIR/node.env
XRAY_PORT=8443

say() { printf '\n\033[1;35m== %s\033[0m\n' "$*" >&2; }
die() { printf '\n\033[1;31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "запустите от root"

CLIENTS=$(cat)
echo "$CLIENTS" | python3 -c 'import json,sys; assert json.load(sys.stdin)["clients"]' 2>/dev/null \
  || die "не получил список людей с главного сервера (первая часть команды)"

say "Ставлю нужные программы"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >&2
apt-get install -y -qq curl unzip python3 debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
if ! command -v caddy >/dev/null; then
  apt-get install -y -qq caddy >/dev/null 2>&1 || {
    curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=$(dpkg --print-architecture)" -o /usr/bin/caddy
    chmod +x /usr/bin/caddy
    id caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
    cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy
After=network-online.target
[Service]
User=caddy
ExecStart=/usr/bin/caddy run --config /etc/caddy/Caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
  }
fi
if ! command -v xray >/dev/null; then
  say "Ставлю Xray (официальный установщик)"
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null 2>&1
fi
command -v xray >/dev/null || die "Xray не установился"

IP=$(curl -fsS -4 --max-time 10 https://api.ipify.org)
DOMAIN="${IP//./-}.sslip.io"

# Обычный сайт-визитка на 443: Reality «маскируется» под него, как на главном сервере.
say "Настраиваю сайт-маскировку $DOMAIN"
mkdir -p /etc/caddy /var/www/raido
echo '<!doctype html><meta charset="utf-8"><title>Raido</title><p>ᚱ</p>' > /var/www/raido/index.html
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	root * /var/www/raido
	file_server
}
EOF
systemctl enable -q caddy
systemctl restart caddy

if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  . "$STATE"
else
  KEYS=$(xray x25519)
  PRIV=$(echo "$KEYS" | awk -F': *' 'tolower($1) ~ /private/ {print $2; exit}')
  PUB=$(echo "$KEYS" | awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}')
  [ -n "$PRIV" ] && [ -n "$PUB" ] || die "не получилось создать ключи: $KEYS"
  SID=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
  XPATH=$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')
  mkdir -p "$DIR"
  (umask 077; printf 'PRIV=%s\nPUB=%s\nSID=%s\nXPATH=%s\n' "$PRIV" "$PUB" "$SID" "$XPATH" > "$STATE")
fi

say "Настраиваю VPN (VLESS XHTTP Reality, порт $XRAY_PORT)"
echo "$CLIENTS" | python3 - "$CFG" "$DOMAIN" "$PRIV" "$SID" "$XPATH" "$XRAY_PORT" <<'PY'
import json, sys
p, domain, priv, sid, path, port = sys.argv[1:]
clients = [{"id": c["id"], "flow": "", "email": c.get("email", "VPN")}
           for c in json.load(sys.stdin)["clients"]]
cfg = {
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "0.0.0.0", "port": int(port), "protocol": "vless",
        "settings": {"clients": clients, "decryption": "none"},
        "streamSettings": {"network": "xhttp", "security": "reality",
            "xhttpSettings": {"path": "/" + path, "mode": "auto"},
            "realitySettings": {"dest": "127.0.0.1:443", "serverNames": [domain],
                                "privateKey": priv, "shortIds": [sid]}},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}}],
    "outbounds": [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "block"}],
    "routing": {"rules": [{"type": "field", "outboundTag": "block", "ip": [
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
        "100.64.0.0/10", "fc00::/7", "fe80::/10", "::1/128"]}]}}
json.dump(cfg, open(p, "w"), indent=2)
PY
chmod 644 "$CFG"
xray run -test -config "$CFG" >/dev/null || die "Xray не принял настройки"

# Как на главном: BBR, большие буферы, без «остывания» соединений после паузы.
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
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow "$XRAY_PORT"/tcp >/dev/null
fi

systemctl enable -q xray
systemctl restart xray; sleep 2
systemctl is-active -q xray || { journalctl -u xray -n 20 --no-pager >&2; die "VPN не запустился"; }
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$DOMAIN/" || true)
[ "$code" = 200 ] || say "Внимание: сайт-маскировка ответил $code (сертификат может ещё получаться, это не страшно)"

say "Готово: второй сервер работает, передаю его параметры главному"
python3 -c 'import json,sys; print(json.dumps(dict(zip(["ip","port","sni","pub","sid","path"], sys.argv[1:]))))' \
  "$IP" "$XRAY_PORT" "$DOMAIN" "$PUB" "$SID" "$XPATH"
