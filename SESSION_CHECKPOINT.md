# Чекпоинт сессии — 04.07.2026

## Проект
`C:\Users\Лек\Desktop\Kimi_Agent_Salon_bot настройка\cosmetology_bot`

Telegram-бот косметолога Ашуры: **@AshuraCosm_bot**

## Что сделано в этой сессии

### Исправления кода
- Ошибка семафора (WinError 121) — `SelectorEventLoop` на Windows
- Прокси Happ/Reality: `utils/proxy.py`
  - `PROXY_AUTO=true` — автоопределение порта (найден `socks5://127.0.0.1:10808`)
  - `PROXY_SSL_VERIFY=false` — отключена проверка SSL для Reality
- Баг `Booking.user_id` vs `telegram_id` в `handlers/client.py`
- `Bot.get_current()` заменён на передачу `bot` из контекста
- Импорт `InlineKeyboardButton`, сохранение `bonus_used`, списание бонусов
- FAQ-пагинация, формат анамнеза, WhatsApp/tel ссылки
- Отступы в `AdminSettings` (`database.py`)

### Запуск
- Точка входа: `bot.py` (не `app.py` изначально — добавлен алиас)
- `app.py` — обёртка для запуска
- `start.bat` — двойной клик для запуска через venv

### Конфиг `.env` (настроено)
```
PROXY_AUTO=true
PROXY_HOST=127.0.0.1
PROXY_PORTS=10808,10809,1080,7890,7891
PROXY_SCHEMES=socks5,http
PROXY_SSL_VERIFY=false
```
+ `BOT_TOKEN` и `ADMIN_ID` (см. файл `.env`)

## Как запустить (продолжение)

1. Включить **Happ** (VPN)
2. Запуск:
   - Двойной клик `start.bat`, или
   - `.\venv\Scripts\python.exe app.py` из папки `cosmetology_bot`
3. Не закрывать окно консоли
4. В Telegram: `/start` в `@AshuraCosm_bot` (админу тоже — иначе «chat not found»)

## Проверено
- Прокси: `socks5://127.0.0.1:10808` — OK
- `getMe` / polling — OK
- Бот стартует и принимает обновления

## Известные моменты
- `chat not found` при уведомлении админа — админ не нажал `/start` в боте
- Системный `python` (3.10) не подходит — только `venv\Scripts\python.exe`
- pip в venv может падать из-за SOCKS — пакеты ставить через системный Python в venv

## Возможные следующие шаги
- Проверить все функции бота в Telegram (запись, админка, бонусы)
- Исправить ADMIN_ID если «chat not found» не уходит после /start
- Деплой на сервер (если нужен 24/7 без ПК)