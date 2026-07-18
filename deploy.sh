#!/bin/bash
set -e
cd "$(dirname "$0")"

# --- Команды: logs / status ---
case "${1:-}" in
  logs)
    echo "Лог бота (выход: Ctrl+C):"
    docker compose logs -f --tail=50 bot
    exit 0
    ;;
  status)
    echo "Состояние контейнера:"
    docker compose ps
    echo ""
    echo "Последние строки лога:"
    docker compose logs --tail=15 bot
    exit 0
    ;;
esac

echo ""
echo "=========================================="
echo "       Деплой AshuraCosm Bot"
echo "=========================================="
echo ""
echo "  (запущено с сервера или через deploy-pc.bat)"
echo ""

# --- [1/6] Архив ---
echo "[1/6] Проверка архива bot.zip..."
if [ -f bot.zip ]; then
  echo "      Нашёл bot.zip — распаковываю..."
  if ! command -v unzip >/dev/null 2>&1; then
    sudo apt install -y unzip
  fi
  unzip -o bot.zip
  echo "      Архив распакован."
else
  echo "      bot.zip нет — использую файлы в этой папке."
fi

# --- [2/6] Docker ---
echo ""
echo "[2/6] Проверка Docker..."
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "      Docker уже установлен."
else
  echo "      Устанавливаю Docker (1–2 минуты)..."
  sudo apt update
  sudo apt install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt update
  sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo systemctl enable docker
  sudo systemctl start docker
  echo "      Docker установлен."
  echo "      Если будет «permission denied» — выйдите из PuTTY и зайдите снова."
fi

# --- [3/6] .env ---
echo ""
echo "[3/6] Проверка файла .env..."
env_ok() {
  [ -f .env ] && grep -qE '^BOT_TOKEN=[^[:space:]]' .env \
    && ! grep -qE '^BOT_TOKEN=(your_|$)' .env \
    && grep -qE '^ADMIN_ID=[0-9]+' .env
}
if env_ok; then
  echo "      .env готов — настройки на месте, nano не нужен."
elif [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
fi
if ! env_ok; then
  echo ""
  echo "  .env не заполнен. Два варианта:"
  echo ""
  echo "  А) С ПК (проще): двойной клик deploy-pc.bat"
  echo "     — загрузит .env автоматически"
  echo ""
  echo "  Б) Вручную в nano:"
  echo "     BOT_TOKEN=..., ADMIN_ID=..., PROXY_AUTO=false"
  echo "     Сохранить: Ctrl+O → Enter → Ctrl+X"
  echo ""
  nano .env
  if ! env_ok; then
    echo "  ОШИБКА: BOT_TOKEN и ADMIN_ID всё ещё не заполнены."
    exit 1
  fi
  echo "      .env сохранён."
fi

# --- [4/6] PROXY_AUTO ---
echo ""
echo "[4/6] Проверка PROXY_AUTO..."
if grep -qiE '^PROXY_AUTO=(true|1|yes|on)' .env; then
  echo ""
  echo "  ОШИБКА: в .env стоит PROXY_AUTO=true"
  echo "  На VPS нужно: PROXY_AUTO=false"
  echo "  Исправьте: nano .env  →  bash deploy.sh"
  exit 1
fi
if ! grep -qiE '^PROXY_AUTO=false' .env; then
  echo ""
  echo "  ОШИБКА: добавьте в .env строку PROXY_AUTO=false"
  echo "  Исправьте: nano .env  →  bash deploy.sh"
  exit 1
fi
echo "      PROXY_AUTO=false — OK."

# --- [5/6] Запуск ---
echo ""
echo "[5/6] Запуск бота..."
mkdir -p data logs
echo "      Папки data/ и logs/ готовы."
echo "      Собираю Docker-образ (первый раз может занять 2–3 минуты)..."
docker compose up -d --build
echo "      Контейнер запущен."

# --- [6/6] Статус ---
echo ""
echo "[6/6] Проверка статуса..."
docker compose ps
echo ""
echo "=========================================="
echo "  Готово! Бот работает 24/7."
echo "=========================================="
echo ""
echo "  Проверьте в Telegram: /start"
echo ""
echo "  Полезные команды:"
echo "    bash deploy.sh status  — состояние бота"
echo "    bash deploy.sh logs    — лог в реальном времени"
echo ""