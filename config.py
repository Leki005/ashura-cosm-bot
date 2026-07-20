"""
Конфигурация бота кабинета косметолога Ашуры.
Все настройки загружаются из .env файла.
"""

import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

# Часовой пояс салона (Астрахань, UTC+4)
SALON_TZ = timezone(timedelta(hours=4))


class Config:
    """Класс конфигурации. Все значения берутся из .env"""

    # --- Токен бота ---
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    # --- ID администратора (Ашура) ---
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
    # OWNER_ID — кому слать уведомления о записях (если не задан, = ADMIN_ID)
    OWNER_ID: int = int(os.getenv("OWNER_ID") or os.getenv("ADMIN_ID", "0"))
    # ERROR_NOTIFY_ID — кому слать уведомления об ошибках и сбоях
    ERROR_NOTIFY_ID: int = int(os.getenv("ERROR_NOTIFY_ID", "0"))
    SENTRY_DSN: str = os.getenv('SENTRY_DSN', '')

    @classmethod
    def owner_id(cls) -> int:
        """ID владельца для уведомлений о записях."""
        return cls.OWNER_ID or cls.ADMIN_ID

    # --- Согласие на обработку персональных данных (152-ФЗ) ---
    PRIVACY_POLICY_VERSION: str = os.getenv("PRIVACY_POLICY_VERSION", "1.0")

    # --- Название салона ---
    SALON_NAME: str = "Кабинет косметолога Ашуры"

    # --- Адрес ---
    SALON_ADDRESS: str = "Астрахань, Кирова 11, 2 этаж, кабинет 6"

    # --- Телефон ---
    SALON_PHONE: str = "89885919401"

    # --- WhatsApp ---
    WHATSAPP_NUMBER: str = "89885919401"

    # --- Instagram ---
    INSTAGRAM_LINK: str = "https://www.instagram.com/ashuracosm"

    # --- Бонусная система ---
    BONUS_PERCENT: int = 5           # Процент бонусов
    BONUS_MIN_AMOUNT: int = 20000    # Минимальная сумма для начисления
    BONUS_MAX_DISCOUNT_PERCENT: int = 50  # Максимальная скидка бонусами (%)

    # --- Запись: минимум минут до приёма (сегодня) ---
    BOOKING_MIN_LEAD_MINUTES: int = 15

    # --- Отмена: минимум часов до приёма (иначе нельзя отменить через бота) ---
    CANCEL_DEADLINE_HOURS: int = int(os.getenv("CANCEL_DEADLINE_HOURS", "2"))

    # --- Напоминания ---
    FOLLOWUP_AFTER_HOURS: int = 1    # Опрос после процедуры (через N часов)
    FOLLOWUP_MAX_AGE_HOURS: int = 48 # Не спрашивать, если визит был давно

    # --- База данных (в Docker: sqlite+aiosqlite:////app/data/bot.db) ---
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db")

    # --- Сеть / прокси (Happ, Reality, V2Ray) ---
    PROXY_URL: str = os.getenv("PROXY_URL", "")  # socks5://127.0.0.1:10808
    PROXY_AUTO: bool = os.getenv("PROXY_AUTO", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    PROXY_HOST: str = os.getenv("PROXY_HOST", "127.0.0.1")
    PROXY_PORTS: tuple[int, ...] = tuple(
        int(p.strip())
        for p in os.getenv("PROXY_PORTS", "10808,10809,1080,7890,7891").split(",")
        if p.strip().isdigit()
    ) or (10808, 10809, 1080, 7890, 7891)
    PROXY_SCHEMES: tuple[str, ...] = tuple(
        s.strip()
        for s in os.getenv("PROXY_SCHEMES", "socks5,http").split(",")
        if s.strip()
    ) or ("socks5", "http")
    # false = отключить проверку SSL (критично для Reality-прокси Happ)
    # ВНИМАНИЕ: отключение SSL делает API-вызовы уязвимыми к MITM. Включите если прокси не Reality.
    PROXY_SSL_VERIFY: bool = os.getenv("PROXY_SSL_VERIFY", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    PROXY_PROBE_TIMEOUT: int = int(os.getenv("PROXY_PROBE_TIMEOUT", "12"))
    TELEGRAM_TIMEOUT: int = int(os.getenv("TELEGRAM_TIMEOUT", "60"))

    # --- Grok API (ИИ-консультант) ---
    XAI_API_KEY: str = os.getenv("XAI_API_KEY", "")
    XAI_MODEL: str = os.getenv("XAI_MODEL", "grok-4.5")
    XAI_BASE_URL: str = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
    XAI_TIMEOUT: int = int(os.getenv("XAI_TIMEOUT", "60"))
    XAI_VISION_MODEL: str = os.getenv("XAI_VISION_MODEL", "grok-4.5")
    AI_HISTORY_LIMIT: int = int(os.getenv("AI_HISTORY_LIMIT", "20"))
    AI_DAILY_LIMIT: int = int(os.getenv("AI_DAILY_LIMIT", "500"))  # Макс. запросов к Grok в день
    AI_DAILY_LIMIT_PER_USER: int = int(os.getenv("AI_DAILY_LIMIT_PER_USER", "30"))
    AI_MAX_CONCURRENT: int = int(os.getenv("AI_MAX_CONCURRENT", "3"))

    # --- Kimi K3 API ---
    KIMI_API_KEY: str = os.getenv("KIMI_API_KEY", "")
    KIMI_MODEL: str = os.getenv("KIMI_MODEL", "kimi-k3")
    KIMI_BASE_URL: str = os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    KIMI_PROXY: str = os.getenv("KIMI_PROXY", "socks5://127.0.0.1:10808")
    KIMI_TIMEOUT: int = int(os.getenv("KIMI_TIMEOUT", "300"))

    # --- Технические ---
    FSM_TTL_MINUTES: int = 10        # Время жизни FSM-состояния
    THROTTLE_RATE: float = 1.0       # 1 сообщение в секунду

    # --- Redis fail-hard ---
    REQUIRE_REDIS: bool = os.getenv('REQUIRE_REDIS', '0').strip().lower() in {'1', 'true', 'yes', 'on'}

    # --- Drop pending updates ---
    DROP_PENDING_UPDATES: bool = os.getenv('DROP_PENDING_UPDATES', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}

    @classmethod
    def validate(cls) -> list[str]:
        """
        Проверяет, что все обязательные настройки заданы.
        Возвращает список ошибок (пустой если всё ок).
        """
        errors = []
        if not cls.BOT_TOKEN:
            errors.append("BOT_TOKEN не задан в .env!")
        if cls.ADMIN_ID == 0:
            errors.append("ADMIN_ID не задан в .env!")
        return errors
