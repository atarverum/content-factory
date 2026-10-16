#!/bin/bash
# Установка фабрики монтажа на Мак: одна команда в Терминале
#   curl -fsSL https://raw.githubusercontent.com/atarverum/content-factory/main/mac/install.sh | bash
# Повторный запуск обновляет фабрику и ничего не ломает.
set -e
say() { printf "\n\033[1;35m▸ %s\033[0m\n" "$1"; }

if [ "$(uname)" != "Darwin" ]; then
  echo "Этот установщик для Мака. Запустите его в Терминале на Маке."; exit 1
fi

if ! command -v brew >/dev/null; then
  say "Ставлю Homebrew (попросит пароль от Мака, буквы при вводе не видны, это нормально)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
[ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"

say "Ставлю программы для видео (ffmpeg, python, node)"
brew install ffmpeg python@3.12 node git >/dev/null

APP="$HOME/.content-factory"
if [ -d "$APP/.git" ]; then
  say "Обновляю фабрику"
  git -C "$APP" pull -q --ff-only
else
  say "Скачиваю фабрику"
  git clone -q https://github.com/atarverum/content-factory "$APP"
fi

say "Ставлю распознавание речи"
PY="$(brew --prefix python@3.12)/bin/python3.12"
[ -x "$APP/venv/bin/python" ] || "$PY" -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "faster-whisper>=1.0" "Pillow>=10"
"$APP/venv/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')" \
  && echo "модель распознавания скачана"

say "Ставлю сборщик черновиков CapCut"
npm install -g -s capcut-cli@0.29 >/dev/null

say "Готовлю папку Фабрика"
F="$HOME/Movies/Фабрика"
mkdir -p "$F/Входящие" "$F/Работа" "$F/Готово" "$F/Библиотека/картинки" "$F/Библиотека/звуки" \
         "$F/Библиотека/музыка" "$F/Библиотека/эффекты"
[ -f "$F/память.md" ] || printf "# Память монтажа\n\nСюда Claude записывает постоянные пожелания Полины и Александра.\n" > "$F/память.md"
LIB_ZIP="$HOME/Downloads/Библиотека.zip"
if [ -f "$LIB_ZIP" ]; then
  unzip -qn "$LIB_ZIP" -d "$F" && echo "библиотека картинок и звуков разложена"   # -n: свои файлы не перезаписываем
elif [ -d "$HOME/Downloads/Библиотека/звуки" ]; then   # Safari сам распаковал архив
  cp -Rn "$HOME/Downloads/Библиотека/." "$F/Библиотека/" && echo "библиотека картинок и звуков разложена"
fi
[ -f "$F/Библиотека/catalog.json" ] || echo "[]" > "$F/Библиотека/catalog.json"

say "Подключаю навык монтажа к Claude"
mkdir -p "$HOME/.claude/skills"
ln -sfn "$APP/mac/skill" "$HOME/.claude/skills/montazh-polina"
if [ ! -f "$F/CLAUDE.md" ]; then
  cat > "$F/CLAUDE.md" <<'EOF'
Это папка монтажа роликов Полины. Для любой просьбы смонтировать или поправить ролик используй навык
montazh-polina. Говори по-русски, коротко и без технических слов.
EOF
fi

capcut doctor >/dev/null 2>&1 && echo "CapCut найден" || echo "CapCut пока не найден: поставьте его из App Store и откройте один раз"

say "Готово!"
echo "Дальше: откройте приложение Claude, вкладка Code, выберите папку «Фильмы → Фабрика» и напишите «Монтируй»."
