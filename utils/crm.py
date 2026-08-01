"""
CRM-модуль: напоминания о повторных визитах + churn-дайджест владельцу.
Защита от спама: авто-отключение после 2 неотвеченных + кнопка "Не напоминать".
"""
import logging
from datetime import datetime, timedelta
from html import escape as html_escape

from sqlalchemy import select, func as sa_func, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram.exceptions import TelegramForbiddenError

from config import Config
from database import Booking, Service, User

logger = logging.getLogger(__name__)

# Константы
REVISIT_REMINDER_DAYS_BEFORE = 3
CHURN_THRESHOLD_30 = 30
CHURN_THRESHOLD_60 = 60
MAX_DIGEST_ITEMS = 15
REVISIT_LOWER_BOUND_DAYS = 30
MAX_NO_RESPONSE = 2  # Авто-отключение после N неотвеченных


async def send_revisit_reminders(bot, session: AsyncSession) -> int:
    """Напоминания клиентам о повторном визите. С кнопкой 'Не напоминать'."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from utils.helpers import now_salon, format_phone, ACTIVE_BOOKING_STATUSES

    now = now_salon()
    reminder_window = now + timedelta(days=REVISIT_REMINDER_DAYS_BEFORE)
    lower_bound = now - timedelta(days=REVISIT_LOWER_BOUND_DAYS)

    result = await session.execute(
        select(User)
        .where(User.next_visit_at.isnot(None))
        .where(User.next_visit_at <= reminder_window)
        .where(User.next_visit_at >= lower_bound)
        .where(User.pd_consent_at.isnot(None))
        .where(User.name.notlike("Удалён_%"))
        .where(User.revisit_reminder_disabled.is_(False))
        .where(User.revisit_reminder_no_response < MAX_NO_RESPONSE)
        .where(
            (User.revisit_reminder_sent_for.is_(None)) |
            (User.revisit_reminder_sent_for != User.next_visit_at)
        )
        # Exclude users with active bookings (subquery avoids N+1)
        .where(~User.id.in_(
            select(Booking.user_id)
            .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
            .distinct()
        ))
    )
    candidates = result.scalars().all()

    sent = 0
    for user in candidates:

        days_until = (user.next_visit_at.date() - now.date()).days
        if days_until < 0:
            days_text = "Уже пора записаться!"
        elif days_until == 0:
            days_text = "Сегодня рекомендуемый день визита!"
        elif days_until == 1:
            days_text = "Завтра рекомендуемый день визита."
        else:
            days_text = f"Через {days_until} дней рекомендуемый день визита."

        text = (
            f"Здравствуйте, {user.name}!\n\n"
            f"Напоминаем: {days_text}\n\n"
            f"Записаться можно прямо здесь — нажмите /start\n\n"
            f"Если не хотите получать напоминания — нажмите кнопку ниже."
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Не напоминать", callback_data=f"crm_no_remind")]
        ])

        try:
            await bot.send_message(
                chat_id=user.telegram_id, text=text, reply_markup=keyboard,
            )
            user.revisit_reminder_sent_for = user.next_visit_at
            # Увеличиваем счётчик неотвеченных. Сбрасывается при новой записи (reset_reminder_on_booking)
            # или при нажатии "Не напоминать". Если клиент ответил — счётчик не мешает (следующее
            # напоминание будет только через revisit_days, к тому моменту бронирование сбросит счётчик).
            user.revisit_reminder_no_response = (user.revisit_reminder_no_response or 0) + 1
            sent += 1
            logger.info("CRM: reminder sent to %s (no_response=%d)", user.telegram_id, user.revisit_reminder_no_response)

            # Google Sheets sync
            try:
                from utils.google_sheets import mark_dirty
                await mark_dirty(session, user.id, reason='crm_reminder')
            except Exception:
                pass

        except TelegramForbiddenError:
            user.revisit_reminder_disabled = True
            logger.info("CRM: user %s blocked the bot, disabling reminders permanently", user.telegram_id)
        except Exception as e:
            logger.warning("CRM: failed to send reminder to %s: %s", user.telegram_id, e)

    await session.flush()
    return sent


async def handle_no_remind(callback, session: AsyncSession) -> None:
    """Клиент нажал 'Не напоминать'."""
    from utils.helpers import get_user_by_telegram_id

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Ошибка.", show_alert=True)
        return

    user.revisit_reminder_disabled = True
    user.revisit_reminder_no_response = 0
    await session.flush()

    await callback.message.edit_text(
        "✅ Хорошо, больше не будем напоминать.\n\n"
        "Если захотите записаться — нажмите /start",
        reply_markup=None,
    )
    await callback.answer("Напоминания отключены")
    logger.info("CRM: reminders disabled by user %s", callback.from_user.id)


async def reset_reminder_on_booking(session: AsyncSession, user_id: int) -> None:
    """Сброс счётчика напоминаний при новом бронировании."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.revisit_reminder_no_response = 0
        user.revisit_reminder_disabled = False
        await session.flush()


async def send_churn_digest(bot, session: AsyncSession) -> int:
    """Дайджест владельцу: клиенты без визитов 30+/60+ дней."""
    from utils.helpers import now_salon, format_phone, ACTIVE_BOOKING_STATUSES

    now = now_salon()

    last_visit_subq = (
        select(Booking.user_id, sa_func.max(Booking.completed_at).label("last_visit"))
        .where(Booking.status == "completed")
        .group_by(Booking.user_id)
        .subquery()
    )

    result = await session.execute(
        select(User, last_visit_subq.c.last_visit)
        .join(last_visit_subq, User.id == last_visit_subq.c.user_id)
        .where(last_visit_subq.c.last_visit.isnot(None))
        .where(User.pd_consent_at.isnot(None))
        .where(User.name.notlike("Удалён_%"))
    )
    rows = result.all()

    bucket_30, bucket_60 = [], []

    for user, last_visit in rows:
        days_since = (now.date() - last_visit.date()).days
        booking_check = await session.execute(
            select(sa_func.count(Booking.id))
            .where(Booking.user_id == user.id)
            .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        )
        if (booking_check.scalar() or 0) > 0:
            continue
        if days_since >= CHURN_THRESHOLD_60 and not user.churn_alert_60_sent:
            bucket_60.append((user, days_since, last_visit))
        elif days_since >= CHURN_THRESHOLD_30 and not user.churn_alert_30_sent:
            bucket_30.append((user, days_since, last_visit))

    if not bucket_30 and not bucket_60:
        return 0

    lines = [f"📊 <b>CRM-дайджест</b> ({now.strftime('%d.%m.%Y')})\n"]

    if bucket_60:
        lines.append(f"🔴 <b>Критично (60+ дней): {len(bucket_60)}</b>")
        for user, days, _ in sorted(bucket_60, key=lambda x: -x[1])[:MAX_DIGEST_ITEMS]:
            lines.append(f"  • {html_escape(user.name)} | {format_phone(user.phone)} | {days}д")
        lines.append("")

    if bucket_30:
        lines.append(f"🟡 <b>Остыли (30+ дней): {len(bucket_30)}</b>")
        for user, days, _ in sorted(bucket_30, key=lambda x: -x[1])[:MAX_DIGEST_ITEMS]:
            lines.append(f"  • {html_escape(user.name)} | {format_phone(user.phone)} | {days}д")
        lines.append("")

    total = len(bucket_30) + len(bucket_60)
    lines.append(f"Всего: {total} клиент(ов) без визитов.")

    text = "\n".join(lines)
    try:
        await bot.send_message(chat_id=Config.ADMIN_ID, text=text, parse_mode="HTML")
        logger.info("CRM: churn digest sent, 30+=%d, 60+=%d", len(bucket_30), len(bucket_60))
        for user, _, _ in bucket_60:
            user.churn_alert_60_sent = True
            user.churn_alert_30_sent = True
        for user, _, _ in bucket_30:
            user.churn_alert_30_sent = True
    except Exception as e:
        logger.warning("CRM: failed to send churn digest: %s", e)

    await session.flush()
    return total
