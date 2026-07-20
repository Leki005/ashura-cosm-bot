"""
Главный файл бота — точка входа.
Собирает все компоненты, регистрирует handlers, middleware,
запускает polling и инициализирует БД.
"""

import asyncio
import logging
import logging.handlers
import os
import sys

# На Windows ProactorEventLoop часто даёт WinError 121 «Превышен таймаут семафора»
# при подключении к api.telegram.org — переключаемся на SelectorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, ErrorEvent
from aiogram.exceptions import TelegramNetworkError
import os as _os
from aiogram.fsm.storage.memory import MemoryStorage
try:
    from aiogram.fsm.storage.redis import RedisStorage
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False
from aiogram.fsm.strategy import FSMStrategy
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from database import apply_migrations, async_session, init_db, seed_data
from handlers import admin, ai_consultant, client, common, privacy, skin_anamnesis
from handlers.privacy import PrivacyConsentMiddleware
from utils.health import check_db
from utils.pii_cleanup import PIICleanup
from utils.proxy import create_telegram_session, resolve_proxy_url, verify_telegram_via_proxy

# =============================================================================
# ЛОГИРОВАНИЕ
# =============================================================================

# Создаём папку для логов
os.makedirs("logs", exist_ok=True)

_log_format = os.getenv("LOG_FORMAT", "text")
_file_handler = logging.handlers.RotatingFileHandler(
    "logs/bot.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
)
_stream_handler = logging.StreamHandler(sys.stdout)

if _log_format == "json":
    from utils.structured_logging import JSONFormatter
    _file_handler.setFormatter(JSONFormatter())
    _stream_handler.setFormatter(JSONFormatter())
else:
    _text_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _file_handler.setFormatter(_text_fmt)
    _stream_handler.setFormatter(_text_fmt)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _stream_handler],
)
logger = logging.getLogger(__name__)

# Audit log — separate file
_audit_handler = logging.handlers.RotatingFileHandler(
    'logs/audit.log', maxBytes=5*1024*1024, backupCount=10, encoding='utf-8'
)
_audit_handler.setLevel(logging.INFO)
_audit_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logging.getLogger('audit').addHandler(_audit_handler)

# Отключаем шумные логи
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# =============================================================================
# УВЕДОМЛЕНИЯ ОБ ОШИБКАХ
# =============================================================================

_error_bot: Bot | None = None
_error_tasks: set[asyncio.Task] = set()
_ERROR_TASKS_MAX = 100  # Защита от утечки памяти


async def notify_error(title: str, details: str) -> None:
    """Отправляет уведомление об ошибке на ERROR_NOTIFY_ID."""
    if not Config.ERROR_NOTIFY_ID or not _error_bot:
        return
    try:
        from html import escape as html_esc
        # Редактируем секреты из traceback
        import re as _re
        redacted = details
        for secret in (Config.BOT_TOKEN, Config.XAI_API_KEY, Config.KIMI_API_KEY,
                       Config.KIMI_PROXY, Config.SENTRY_DSN):
            if secret and len(secret) > 4:
                redacted = redacted.replace(secret, secret[:3] + "***")
        redacted = _re.sub(r'Bearer\s+\S+', 'Bearer ***REDACTED***', redacted)
        redacted = _re.sub(r'Authorization:\s*\S+', 'Authorization: ***REDACTED***', redacted)
        escaped_details = html_esc(redacted)
        text = (
            f"🚨 <b>{html_esc(title)}</b>\n\n"
            f"<pre>{escaped_details[:3000]}</pre>"
        )
        await _error_bot.send_message(
            chat_id=Config.ERROR_NOTIFY_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.debug("Не удалось отправить уведомление об ошибке: %s", e)


class ErrorNotifyHandler(logging.Handler):
    """Лог-хэндлер: отправляет ERROR-сообщения в Telegram."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        if not Config.ERROR_NOTIFY_ID:
            return
        # Очистка завершённых/зависших задач
        _error_tasks.difference_update(t for t in _error_tasks if t.done())
        if len(_error_tasks) >= _ERROR_TASKS_MAX:
            return
        msg = self.format(record)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(notify_error("Ошибка в боте", msg))
            _error_tasks.add(task)
            task.add_done_callback(_error_tasks.discard)
        except (RuntimeError, Exception):
            pass


# Подключаем ErrorNotifyHandler к корневому логгеру
_error_handler = ErrorNotifyHandler()
_error_handler.setLevel(logging.ERROR)
_error_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
logging.getLogger().addHandler(_error_handler)

# Optional Sentry integration
if Config.SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=Config.SENTRY_DSN,
            traces_sample_rate=0.1,
            environment='production',
        )
        logger.info('Sentry initialized')
    except ImportError:
        logger.warning('SENTRY_DSN set but sentry-sdk not installed')
    except Exception as e:
        logger.warning('Sentry init failed: %s', e)

# Глобальный шедулер для напоминаний
scheduler: AsyncIOScheduler | None = None


# =============================================================================
# MIDDLEWARE: Сессия БД
# =============================================================================

class DbSessionMiddleware:
    """
    Middleware, которая открывает сессию БД для каждого update.
    Сессия передаётся в handler через kwargs.
    """

    async def __call__(self, handler, event, data):
        async with async_session() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                # Гарантируем сохранение изменений (отмена записи и т.д.)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


# =============================================================================
# MIDDLEWARE: Throttling
# =============================================================================

from aiogram.types import CallbackQuery as _CbType, Message as _MsgType
from utils.helpers import check_throttle, throttled_message


class ThrottlingMiddleware:
    """Защита от спама — не более 1 сообщения в секунду."""

    async def __call__(self, handler, event, data):
        # Update.event — это Message | CallbackQuery | и т.д.
        user_id = None
        inner_event = getattr(event, "event", None)
        if inner_event and hasattr(inner_event, "from_user") and inner_event.from_user:
            user_id = inner_event.from_user.id

        admin_ids = {aid for aid in (Config.ADMIN_ID, Config.owner_id()) if aid}
        if user_id and user_id not in admin_ids and await check_throttle(user_id):
            # Игнорируем спам (для callback просто отвечаем)
            if isinstance(inner_event, _CbType):
                try:
                    await inner_event.answer("⏳ Не спешите! Подождите секунду.", show_alert=True)
                except Exception:
                    pass
            elif isinstance(inner_event, _MsgType):
                try:
                    await throttled_message(inner_event)
                except Exception:
                    pass
            return None

        return await handler(event, data)


# =============================================================================
# НАПОМИНАНИЯ (APScheduler)
# =============================================================================

async def send_reminders(bot: Bot) -> None:
    """
    Проверяет и отправляет напоминания клиентам.
    Вызывается каждые 5 минут шедулером.
    """
    try:
        await _send_reminders_inner(bot)
    except Exception:
        logger.exception("send_reminders: непредвиденная ошибка")


async def _send_reminders_inner(bot: Bot) -> None:
    from datetime import datetime, timedelta, timezone
    from database import Booking, User
    from utils.helpers import format_phone, now_salon

    now = now_salon()

    from sqlalchemy.orm import joinedload

    async with async_session() as session:
        # Находим подтверждённые записи (только ближайшие 48 часов)
        # Сначала грузим всё, потом фильтруем в Python (строковые даты DD.MM.YYYY несортируемы)
        result = await session.execute(
            select(Booking)
            .options(joinedload(Booking.user), joinedload(Booking.service))
            .where(Booking.status == "confirmed")
            .where(Booking.preferred_date.isnot(None))
        )
        all_bookings = result.scalars().unique().all()

        max_horizon = now + timedelta(hours=48, minutes=5)

        def _parse_booking_date(b):
            from datetime import datetime as dt
            date_str = (b.preferred_date or "").strip()
            if not date_str:
                return None
            time_str = (b.preferred_time or "").strip()
            for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
                try:
                    full = f"{date_str} {time_str}".strip() if time_str else date_str
                    return dt.strptime(full, fmt)
                except ValueError:
                    continue
            return None

        bookings = []
        for b in all_bookings:
            bdt = _parse_booking_date(b)
            if bdt and bdt <= max_horizon:
                bookings.append(b)

        for booking in bookings:
            # Парсим дату/время из строки (упрощённо)
            date_str = booking.preferred_date or ""
            time_str = booking.preferred_time or ""

            # Пропускаем если нет валидной даты
            if not date_str:
                continue

            # Пытаемся распарсить дату
            try:
                # Форматы: "15.06.2024 14:00" или "15.06.2024"
                from datetime import datetime as dt

                booking_dt = None
                for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
                    try:
                        full_str = f"{date_str} {time_str}".strip() if time_str else date_str
                        booking_dt = dt.strptime(full_str, fmt)
                        break
                    except ValueError:
                        continue

                if booking_dt is None:
                    continue
            except Exception:
                continue

            # Проверяем напоминание за 24 часа (с catch-up: если до визита < 24ч и не отправлено)
            time_to_booking = booking_dt - now
            if not booking.reminder_24h_sent and timedelta(minutes=0) < time_to_booking <= timedelta(hours=24, minutes=5):
                await _send_reminder_24h(bot, session, booking)

            # Проверяем напоминание за 2 часа (с catch-up: если до визита < 2ч и не отправлено)
            if not booking.reminder_2h_sent and timedelta(minutes=0) < time_to_booking <= timedelta(hours=2, minutes=5):
                await _send_reminder_2h(bot, session, booking)

        from utils.helpers import now_salon
        await _send_post_procedure_followups(bot, session, now_salon())

        await session.commit()


async def _send_post_procedure_followups(bot: Bot, session, now) -> None:
    """Опрос клиента ~через 1 час после завершённой процедуры."""
    from datetime import timedelta

    from sqlalchemy.orm import joinedload

    from database import Booking, User
    from keyboards import post_procedure_feedback_keyboard
    from utils.helpers import format_booking_services_line

    delay = timedelta(hours=Config.FOLLOWUP_AFTER_HOURS)
    max_age = timedelta(hours=Config.FOLLOWUP_MAX_AGE_HOURS)

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.user), joinedload(Booking.service))
        .where(Booking.status == "completed")
        .where(Booking.completed_at.isnot(None))
        .where(Booking.followup_sent_at.is_(None))
    )
    bookings = result.scalars().unique().all()

    for booking in bookings:
        elapsed = now - booking.completed_at
        if elapsed < delay:
            continue
        if elapsed > max_age:
            booking.followup_sent_at = now
            await session.flush()  # Flush сразу, не в конце
            continue

        user = booking.user
        if not user:
            continue

        from html import escape as html_esc
        service_name = format_booking_services_line(booking)
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"💫 <b>Здравствуйте, {html_esc(user.name)}!</b>\n\n"
                    f"Прошёл примерно час после вашей процедуры:\n"
                    f"💅 <b>{html_esc(service_name)}</b>\n\n"
                    f"Как всё прошло? Всё ли вам понравилось?\n"
                    f"Ваш ответ поможет Ашуре заботиться о вас ещё лучше 💛"
                ),
                reply_markup=post_procedure_feedback_keyboard(booking.id),
                parse_mode="HTML",
            )
            booking.followup_sent_at = now
            await session.flush()  # Flush сразу после каждого успешного отправления
            logger.info(
                "Опрос после процедуры #%s отправлен клиенту %s",
                booking.id, user.telegram_id,
            )
        except Exception as e:
            logger.warning(
                "Не удалось отправить опрос после процедуры #%s: %s",
                booking.id, e,
            )


async def _send_reminder_24h(bot: Bot, session, booking) -> None:
    """Отправляет напоминание за 24 часа."""
    from html import escape as html_esc
    from utils.helpers import format_booking_services_line
    user = booking.user
    if not user:
        return
    service_name = format_booking_services_line(booking)

    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"⏰ <b>Напоминание!</b>\n\n"
                f"Завтра у вас запись к Ашуре!\n\n"
                f"💅 <b>Услуга:</b> {html_esc(service_name)}\n"
                f"📅 <b>Дата:</b> {html_esc(booking.preferred_date or '—')}\n"
                f"⏰ <b>Время:</b> {html_esc(booking.preferred_time or '—')}\n\n"
                f"📍 <b>Адрес:</b> {Config.SALON_ADDRESS}\n\n"
                f"Ждём вас! 💫"
            ),
            parse_mode="HTML",
        )
        booking.reminder_24h_sent = True
        await session.flush()
        logger.info("Напоминание 24ч отправлено %s", user.telegram_id)
    except Exception as e:
        logger.warning("Не удалось отправить напоминание 24ч %s: %s", user.telegram_id, e)


async def _send_reminder_2h(bot: Bot, session, booking) -> None:
    """Отправляет напоминание за 2 часа."""
    from html import escape as html_esc
    from utils.helpers import format_booking_services_line
    user = booking.user
    if not user:
        return
    service_name = format_booking_services_line(booking)

    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"⏰ <b>Напоминание!</b>\n\n"
                f"Через 2 часа ваш приём!\n\n"
                f"💅 <b>Услуга:</b> {html_esc(service_name)}\n"
                f"📅 <b>Время:</b> {html_esc(booking.preferred_time or '—')}\n\n"
                f"📍 <b>Адрес:</b> {Config.SALON_ADDRESS}\n\n"
                f"До встречи! 💫"
            ),
            parse_mode="HTML",
        )
        booking.reminder_2h_sent = True
        await session.flush()
        logger.info("Напоминание 2ч отправлено %s", user.telegram_id)
    except Exception as e:
        logger.warning("Не удалось отправить напоминание 2ч %s: %s", user.telegram_id, e)


async def send_reviews_to_moderation(bot: Bot) -> None:
    """Отправляет админу отзывы, у которых истёк 30-минутный окно редактирования."""
    try:
        await _send_reviews_to_moderation_inner(bot)
    except Exception:
        logger.exception("send_reviews_to_moderation: непредвиденная ошибка")


async def _send_reviews_to_moderation_inner(bot: Bot) -> None:
    from datetime import datetime, timedelta, timezone
    from sqlalchemy.orm import joinedload
    from database import Review, User
    from keyboards import admin_review_moderation_keyboard
    from utils.helpers import now_salon

    edit_window = timedelta(minutes=30)
    now = now_salon()

    async with async_session() as session:
        result = await session.execute(
            select(Review)
            .options(joinedload(Review.user))
            .where(Review.is_published == False)
            .where(Review.notified_admin == False)
        )
        reviews = result.scalars().unique().all()

        for review in reviews:
            if now - review.created_at < edit_window:
                continue

            # Окно редактирования истекло — отправляем на модерацию
            user = review.user
            if not user:
                continue

            try:
                from html import escape as html_esc
                await bot.send_message(
                    chat_id=Config.ADMIN_ID,
                    text=(
                        f"★ <b>Отзыв на модерацию</b>\n\n"
                        f"👤 {html_esc(user.name)}\n"
                        f"{'★' * review.rating}\n"
                        f"{html_esc(review.text) if review.text else 'Без текста'}"
                    ),
                    reply_markup=admin_review_moderation_keyboard(review.id),
                )
                review.notified_admin = True
                logger.info("Отзыв #%s отправлен на модерацию", review.id)
            except Exception as e:
                logger.warning("Не удалось отправить отзыв #%s на модерацию: %s", review.id, e)

        await session.commit()


# =============================================================================
# ИНИЦИАЛИЗАЦИЯ
# =============================================================================

async def on_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    """Вызывается при старте бота."""
    global scheduler, _error_bot
    _error_bot = bot
    logger.info("=== БОТ ЗАПУСКАЕТСЯ ===")
    logger.info("Салон: %s", Config.SALON_NAME)
    logger.info("Админ ID: %s", Config.ADMIN_ID)

    # Команды бота (меню «/» в Telegram)
    await bot.set_my_commands([
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="start", description="Старт / приветствие"),
        BotCommand(command="restart", description="Сбросить диалог"),
        BotCommand(command="help", description="Помощь"),
    ])

    # Сбрасываем возможные зависшие webhook перед polling
    try:
        await bot.delete_webhook(drop_pending_updates=Config.DROP_PENDING_UPDATES)
        logger.info("Webhook сброшен (delete_webhook)")
    except Exception as e:
        logger.warning("Не удалось сбросить webhook: %s", e)

    # Проверяем конфиг
    errors = Config.validate()
    if errors:
        for err in errors:
            logger.error("КОНФИГ: %s", err)
        sys.exit(1)

    # Инициализируем БД
    await init_db()
    async with async_session() as session:
        await seed_data(session)
        await apply_migrations(session)

    # Healthcheck: БД
    if await check_db():
        logger.info('Healthcheck: DB OK')
    else:
        logger.error('Healthcheck: DB FAIL')
        sys.exit(1)

    # Запускаем шедулер
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_reminders,
        "interval",
        minutes=5,
        args=[bot],
        id="reminders",
        replace_existing=True,
        max_instances=1,  # Защита от параллельных запусков
    )
    scheduler.add_job(
        send_reviews_to_moderation,
        "interval",
        minutes=2,
        args=[bot],
        id="review_moderation",
        replace_existing=True,
        max_instances=1,  # Защита от параллельных запусков
    )
    scheduler.add_job(
        PIICleanup.run_all,
        "cron",
        day_of_week="sun",
        hour=3,
        minute=0,
        id="pii_cleanup",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Шедулер запущен: напоминания (5 мин), модерация отзывов (2 мин), PII cleanup (вс 3:00).")

    # Уведомляем админа о запуске
    logger.info("=== БОТ ГОТОВ К РАБОТЕ ===")


async def on_shutdown(bot: Bot, dispatcher: Dispatcher) -> None:
    """Вызывается при остановке бота."""
    global scheduler, _error_bot
    logger.info("=== БОТ ОСТАНАВЛИВАЕТСЯ ===")

    # Останавливаем шедулер
    if scheduler:
        scheduler.shutdown()
        logger.info("Шедулер остановлен.")

    # Закрываем соединения с БД
    from database import engine
    await engine.dispose()
    logger.info("DB engine disposed.")

    # Закрываем переиспользуемую HTTP-сессию Grok API
    from utils import grok as grok_module
    if grok_module._grok_session is not None and not grok_module._grok_session.closed:
        await grok_module._grok_session.close()
        logger.info("Grok HTTP-сессия закрыта.")

    # Закрываем HTTP-сессию Kimi API
    from utils import kimi as kimi_module
    await kimi_module.close_kimi_session()
    logger.info("Kimi HTTP-сессия закрыта.")

    _error_bot = None


# =============================================================================
# MAIN
# =============================================================================

def create_bot(proxy_url: str | None, ssl_verify: bool) -> Bot:
    """Создаёт экземпляр бота с настроенной HTTP-сессией и прокси."""
    session = create_telegram_session(Config, proxy_url, ssl_verify=ssl_verify)
    return Bot(
        token=Config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def start_polling_with_retry(dp: Dispatcher, bot: Bot, max_retries: int = 5) -> None:
    """Запускает polling с повторными попытками при сетевых ошибках."""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Запуск polling (попытка %s/%s)...", attempt, max_retries)
            await dp.start_polling(bot)
            return
        except TelegramNetworkError as e:
            if attempt >= max_retries:
                logger.error(
                    "Не удалось подключиться к Telegram после %s попыток. "
                    "Проверьте интернет, VPN или задайте PROXY_URL в .env",
                    max_retries,
                )
                raise
            delay = attempt * 5
            logger.warning(
                "Сетевая ошибка (попытка %s/%s): %s. Повтор через %s сек...",
                attempt,
                max_retries,
                e,
                delay,
            )
            await asyncio.sleep(delay)


async def main() -> None:
    """Главная функция — инициализация и запуск."""

    proxy_url, ssl_verify = await resolve_proxy_url(Config)
    if not proxy_url and Config.PROXY_AUTO:
        logger.error(
            "PROXY_AUTO=true, но рабочий прокси не найден. "
            "Запустите Happ/VPN или укажите PROXY_URL=socks5://127.0.0.1:10808"
        )
        sys.exit(1)

    await verify_telegram_via_proxy(
        Config.BOT_TOKEN,
        proxy_url,
        ssl_verify=ssl_verify,
        timeout=float(Config.TELEGRAM_TIMEOUT),
    )

    bot = create_bot(proxy_url, ssl_verify)

    # Создаём диспетчер с FSM
        # FSM storage: Redis if available, MemoryStorage as fallback
    redis_url = _os.getenv('REDIS_URL', '')
    if redis_url and _REDIS_AVAILABLE:
        try:
            storage = RedisStorage.from_url(redis_url, ttl=Config.FSM_TTL_MINUTES * 60)
            logger.info('FSM storage: Redis (%s), TTL=%d min', redis_url, Config.FSM_TTL_MINUTES)
        except Exception as e:
            if Config.REQUIRE_REDIS:
                logger.error('Redis REQUIRED but unavailable: %s', e)
                sys.exit(1)
            logger.warning('Redis unavailable (%s), falling back to MemoryStorage', e)
            storage = MemoryStorage()
    else:
        if Config.REQUIRE_REDIS:
            logger.error('REQUIRE_REDIS=1 but no REDIS_URL set')
            sys.exit(1)
        storage = MemoryStorage()
        logger.info('FSM storage: MemoryStorage (no REDIS_URL set)')
    dp = Dispatcher(storage=storage, fsm_strategy=FSMStrategy.USER_IN_CHAT)

    # Глобальный обработчик ошибок
    @dp.error()
    async def on_error(event: ErrorEvent) -> bool:
        """Ловит все необработанные исключения и уведомляет ERROR_NOTIFY_ID."""
        exception = event.exception
        tb = "".join(
            __import__("traceback").format_exception(type(exception), exception, exception.__traceback__)
        )
        logger.error("Необработанная ошибка: %s\n%s", exception, tb)
        await notify_error("Необработанная ошибка", tb)
        # Отправляем клиенту честное сообщение об ошибке
        try:
            update = event.update
            if update.message:
                await update.message.answer(
                    "⚠️ Произошла ошибка. Попробуйте ещё раз или нажмите /menu."
                )
            elif update.callback_query:
                await update.callback_query.answer(
                    "⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True,
                )
        except Exception:
            pass
        return True  # Подавляем повторный raise

    # Регистрируем middleware
    dp.update.middleware(ThrottlingMiddleware())
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(PrivacyConsentMiddleware())

    # Админ-роутер первым — чтобы client-хендлеры (svc_ и т.д.) не перехватывали admin_*
    dp.include_router(common.router)
    dp.include_router(privacy.router)
    dp.include_router(admin.router)
    dp.include_router(ai_consultant.router)
    dp.include_router(skin_anamnesis.router)
    dp.include_router(client.router)

    # Обработчики жизненного цикла
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await start_polling_with_retry(dp, bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем (Ctrl+C).")
    except Exception as e:
        logger.exception("Критическая ошибка: %s", e)
        sys.exit(1)
