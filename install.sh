#!/usr/bin/env bash
# Установка контент-фабрики на чистый сервер Ubuntu 22.04/24.04 (Timeweb Cloud, регион Нидерланды).
#   curl -fsSL https://raw.githubusercontent.com/<аккаунт>/<репозиторий>/main/install.sh | sudo bash
# Повторный запуск безопасен: обновит код и спросит только то, чего ещё нет.
set -euo pipefail

REPO="${REPO:-https://github.com/atarverum/content-factory.git}"
BRANCH="${BRANCH:-main}"
APP=/opt/factory
DATA=/var/lib/factory
ENV=/etc/factory.env

say() { printf '\n\033[1;35m▶ %s\033[0m\n' "$*"; }
ask() {  # ask ПЕРЕМЕННАЯ "вопрос" [secret]
  local var=$1 prompt=$2 secret=${3:-} val=""
  if [ -n "$secret" ]; then read -r -s -p "$prompt: " val </dev/tty; echo; else read -r -p "$prompt: " val </dev/tty; fi
  printf -v "$var" '%s' "$val"
}
setenv() {  # setenv КЛЮЧ ЗНАЧЕНИЕ: записать в /etc/factory.env
  touch "$ENV"; chmod 600 "$ENV"
  grep -v "^$1=" "$ENV" > "$ENV.tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$ENV.tmp"; mv "$ENV.tmp" "$ENV"; chmod 600 "$ENV"
}
getenv() { [ -f "$ENV" ] && grep "^$1=" "$ENV" | head -1 | cut -d= -f2- || true; }

[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }

say "Ставлю системные программы (ffmpeg, python, caddy)"
export DEBIAN_FRONTEND=noninteractive
# Старый репозиторий Caddy (cloudsmith) стал платным и ломает apt update: убираем его, Caddy есть в Ubuntu.
rm -f /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq
apt-get install -y -qq ffmpeg python3-venv python3-pip git curl ca-certificates debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
if ! command -v caddy >/dev/null; then
  apt-get install -y -qq caddy >/dev/null 2>&1 || {
    # В Ubuntu 22.04 Caddy нет в стандартных репозиториях: берём готовую программу с сайта Caddy.
    curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=$(dpkg --print-architecture)" -o /usr/bin/caddy
    chmod +x /usr/bin/caddy
    id caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
    mkdir -p /etc/caddy
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
    systemctl daemon-reload && systemctl enable -q caddy
  }
fi
mkdir -p /etc/caddy

# Распознаванию речи нужно ~2 ГБ памяти: на маленьком сервере добавим файл подкачки.
if [ "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" -lt 3500 ] && ! swapon --show | grep -q /swapfile; then
  say "Добавляю 2 ГБ подкачки"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

say "Скачиваю код фабрики"
if [ -d "$APP/.git" ]; then git -C "$APP" pull -q origin "$BRANCH"; else git clone -q -b "$BRANCH" "$REPO" "$APP"; fi
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
mkdir -p "$DATA" && chmod 700 "$DATA"

say "Настройки (вводимые ключи не показываются на экране и не уходят никуда, кроме этого сервера)"
if [ -z "$(getenv BOT_TOKEN)" ]; then ask TOKEN "Токен бота от @BotFather" secret; setenv BOT_TOKEN "$TOKEN"; fi
if [ -z "$(getenv ANTHROPIC_API_KEY)" ]; then
  ask KEY "Ключ Claude API для подписей (Enter, чтобы пропустить: будут шаблонные подписи)" secret
  [ -n "$KEY" ] && setenv ANTHROPIC_API_KEY "$KEY"
fi
IP=$(curl -fsS https://api.ipify.org)
setenv PUBLIC_URL "https://${IP//./-}.sslip.io"
setenv FACTORY_DATA "$DATA"
[ -n "$(getenv PUBLISH_TIME)" ] || setenv PUBLISH_TIME "19:00"

# Локальный сервер Telegram снимает ограничение 20 МБ на видео. Нужны api_id и api_hash с my.telegram.org.
if [ -z "$(getenv TELEGRAM_API_URL)" ]; then
  ask API_ID "api_id с my.telegram.org для видео больше 20 МБ (Enter, чтобы пропустить)"
  if [ -n "$API_ID" ]; then
    ask API_HASH "api_hash" secret
    say "Запускаю локальный сервер Telegram"
    command -v docker >/dev/null || apt-get install -y -qq docker.io >/dev/null
    mkdir -p /var/lib/telegram-bot-api
    docker rm -f tg-bot-api >/dev/null 2>&1 || true
    docker run -d --name tg-bot-api --restart always -p 127.0.0.1:8081:8081 \
      -e TELEGRAM_API_ID="$API_ID" -e TELEGRAM_API_HASH="$API_HASH" -e TELEGRAM_LOCAL=1 \
      -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api aiogram/telegram-bot-api:latest >/dev/null
    # Бот переезжает с облачного сервера Telegram на локальный.
    curl -fsS "https://api.telegram.org/bot$(getenv BOT_TOKEN)/logOut" >/dev/null || true
    setenv TELEGRAM_API_URL "http://127.0.0.1:8081"
  fi
fi

say "Скачиваю модель распознавания речи (один раз)"
"$APP/.venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

say "Настраиваю https-адрес $(getenv PUBLIC_URL)"
if grep -qs '^PORT=443$' /usr/local/etc/xray/vpn.env; then
  # Порт 443 занят личным VPN (vpn/install-vpn.sh): сайт живёт за ним на 127.0.0.1:8444.
  cat > /etc/caddy/Caddyfile <<EOF
{
	https_port 8444
	auto_https disable_redirects
}
http://${IP//./-}.sslip.io {
	redir https://${IP//./-}.sslip.io{uri} permanent
}
${IP//./-}.sslip.io:8444 {
	bind 127.0.0.1
	handle_path /sub/* {
		# Happ сам обновляет подписку раз в час.
		header Content-Type "application/json; charset=utf-8"
		header profile-update-interval "1"
		root * /etc/xray-sub
		file_server
	}
	handle {
		reverse_proxy 127.0.0.1:8080
	}
}
EOF
else
  cat > /etc/caddy/Caddyfile <<EOF
${IP//./-}.sslip.io {
	reverse_proxy 127.0.0.1:8080
}
EOF
fi
systemctl reload caddy || systemctl restart caddy

say "Запускаю бота как службу"
cat > /etc/systemd/system/factory.service <<EOF
[Unit]
Description=Контент-фабрика (Telegram-бот)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$ENV
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -m factory.bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Автообновление: раз в 15 минут проверяем GitHub, при новом коде обновляемся и перезапускаемся.
cat > /usr/local/bin/factory-update <<EOF
#!/usr/bin/env bash
set -e
cd $APP
git fetch -q origin $BRANCH
if [ "\$(git rev-parse HEAD)" != "\$(git rev-parse origin/$BRANCH)" ]; then
  git reset -q --hard origin/$BRANCH
  $APP/.venv/bin/pip install -q -r requirements.txt
  systemctl restart factory
fi
EOF
chmod +x /usr/local/bin/factory-update
cat > /etc/systemd/system/factory-update.service <<EOF
[Unit]
Description=Обновление контент-фабрики
[Service]
Type=oneshot
ExecStart=/usr/local/bin/factory-update
EOF
cat > /etc/systemd/system/factory-update.timer <<EOF
[Unit]
Description=Проверка обновлений контент-фабрики
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable -q --now factory-update.timer
systemctl enable -q factory
systemctl restart factory

sleep 8
if ! systemctl is-active -q factory; then
  echo "Бот не запустился. Последние строки журнала:"; journalctl -u factory -n 30 --no-pager; exit 1
fi
say "Готово!"
echo "Адрес для настроек Meta (Valid OAuth Redirect URI): $(getenv PUBLIC_URL)/ig/callback"
if [ -s "$DATA/pair_code.txt" ]; then
  echo "Откройте своего бота в Telegram и отправьте:  /start $(cat "$DATA/pair_code.txt")"
fi
