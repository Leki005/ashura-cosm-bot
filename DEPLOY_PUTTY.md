# Деплой бота

## С ПК (рекомендуется)

1. Заполнить `.env` (BOT_TOKEN, ADMIN_ID)
2. **`deploy-pc.bat`** → заполнить `deploy.local` (IP сервера)
3. **`deploy-pc.bat`** → настроить SSH-ключ (пароль один раз)
4. Дальше только **`deploy-pc.bat`**

## Обновление
**`deploy-pc.bat`**

## SSH-ключ
Скрипт предложит настроить сам. Пароль в файлы не сохраняется.

## На сервере
`bash deploy.sh` | `bash deploy.sh status` | `bash deploy.sh logs`