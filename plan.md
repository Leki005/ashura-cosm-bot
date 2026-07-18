# План проекта — Бот косметолога Ашуры

## Архитектура
- Python 3.11+, aiogram 3.x, SQLAlchemy 2.x, SQLite
- FSM для многошаговых процессов
- APScheduler для напоминаний
- python-dotenv для конфигурации

## Структура
```
cosmetology_bot/
├── .env                    # Конфиг с реальными данными
├── .env.example            # Шаблон
├── bot.py                  # Точка входа
├── config.py               # Настройки
├── database.py             # БД (SQLite + SQLAlchemy)
├── keyboards.py            # Все кнопки
├── requirements.txt
├── handlers/
│   ├── __init__.py
│   ├── client.py           # Клиент: меню, запись, анамнез, отзывы, FAQ, бонусы, контакты
│   ├── admin.py            # Админ: заявки, статистика, рассылка, модерация, FAQ
│   └── common.py           # Старт, регистрация, помощь
└── utils/
    ├── __init__.py
    ├── states.py           # FSM-состояния
    └── helpers.py          # Вспомогательные функции
```

## Модули
1. **config.py** — BOT_TOKEN, ADMIN_ID, настройки салона
2. **database.py** — User, Service, Booking, Review, FAQ, BonusTransaction
3. **keyboards.py** — inline-кнопки для всех экранов
4. **states.py** — Registration, Anamnesis, Booking, Review, Consultation
5. **helpers.py** — throttling, валидаторы, форматтеры
6. **common.py** — /start, регистрация (имя + телефон)
7. **client.py** — главное меню, запись, анамнез, услуги, отзывы, FAQ, бонусы, контакты, консультация
8. **admin.py** — /admin, заявки, статистика, рассылка, модерация отзывов, FAQ, бонусы
9. **bot.py** — dispatcher, routers, middleware, запуск

## Дополнительные фичи
- Консультация по фото (пересылка Ашуре)
- Диалог "Задать вопрос" (пересылка Ашуре с кнопкой Ответить)
- Показать на карте (Яндекс.Карты)
- Кнопка Позвонить
- История записей клиента
- Throttling middleware
- Логирование в файл
- Превью рассылки
- Graceful restart
- Полная статистика
