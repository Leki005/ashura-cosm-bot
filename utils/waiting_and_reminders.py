"""
Фича 1: Умный лист ожидания.
Фича 2: Авто-памятки (До/После процедуры).
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from database import WaitingList, Booking, Service, User, SALON_TZ

logger = logging.getLogger(__name__)


# =============================================================================
# ФИЧА 1: ЛИСТ ОЖИДАНИЯ
# =============================================================================

async def add_to_waiting_list(
    session: AsyncSession, user_id: int, service_id: Optional[int], preferred_date: str
) -> WaitingList:
    """Добавляет клиента в лист ожидания."""
    existing = await session.execute(
        select(WaitingList).where(
            WaitingList.user_id == user_id,
            WaitingList.preferred_date == preferred_date,
            WaitingList.status == "waiting",
        )
    )
    if existing.scalar_one_or_none():
        return None  # Уже в очереди

    entry = WaitingList(
        user_id=user_id,
        service_id=service_id,
        preferred_date=preferred_date,
        status="waiting",
    )
    session.add(entry)
    await session.flush()
    return entry


async def get_waiting_users_for_date(
    session: AsyncSession, preferred_date: str
) -> list:
    """Возвращает список пользователей в листе ожидания на дату."""
    result = await session.execute(
        select(WaitingList, User)
        .join(User, WaitingList.user_id == User.id)
        .where(
            WaitingList.preferred_date == preferred_date,
            WaitingList.status == "waiting",
        )
        .order_by(WaitingList.created_at)
    )
    return result.all()


async def notify_waiting_users(
    session: AsyncSession, bot, preferred_date: str, preferred_time: str, service_name: str
) -> int:
    """Уведомляет пользователей в листе ожидания о свободном слоте."""
    waiting = await get_waiting_users_for_date(session, preferred_date)
    if not waiting:
        return 0

    notified = 0
    for entry, user in waiting:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Забрать слот!",
                    callback_data=f"waitlist_take_{entry.id}_{preferred_date}_{preferred_time}"
                )],
                [InlineKeyboardButton(text="❌ Не нужно", callback_data=f"waitlist_skip_{entry.id}")],
            ])

            await bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"🔔 <b>Освободилось окно!</b>\n\n"
                    f"📅 Дата: {preferred_date}\n"
                    f"⏰ Время: {preferred_time}\n"
                    f"💅 Услуга: {service_name}\n\n"
                    f"Кто первый нажал — того и запись!"
                ),
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            entry.status = "notified"
            entry.notified_at = datetime.now(SALON_TZ).replace(tzinfo=None)
            notified += 1
        except Exception as e:
            logger.warning(f"Не удалось уведомить {user.telegram_id}: {e}")

    await session.flush()
    return notified


async def mark_slot_taken(session: AsyncSession, entry_id: int) -> bool:
    """Помечает слот как занятый."""
    result = await session.execute(
        select(WaitingList).where(WaitingList.id == entry_id, WaitingList.status == "notified")
    )
    entry = result.scalar_one_or_none()
    if not entry:
        return False
    entry.status = "booked"
    await session.flush()
    return True


async def mark_slot_skipped(session: AsyncSession, entry_id: int) -> bool:
    """Помечает слот как пропущенный."""
    result = await session.execute(
        select(WaitingList).where(WaitingList.id == entry_id, WaitingList.status == "notified")
    )
    entry = result.scalar_one_or_none()
    if not entry:
        return False
    entry.status = "expired"
    await session.flush()
    return True


async def cleanup_expired_waiting(session: AsyncSession) -> int:
    """Очищает устаревшие записи из листа ожидания (старше 7 дней)."""
    from datetime import timedelta
    cutoff = datetime.now(SALON_TZ).replace(tzinfo=None) - timedelta(days=7)
    result = await session.execute(
        select(WaitingList).where(
            WaitingList.status.in_(["waiting", "notified"]),
            WaitingList.created_at < cutoff,
        )
    )
    expired = result.scalars().all()
    for entry in expired:
        entry.status = "expired"
    await session.flush()
    return len(expired)


# =============================================================================
# ФИЧА 2: АВТО-ПАМЯТКИ (ДО/ПОСЛЕ)
# =============================================================================

# Маппинг ключевых слов → тип процедуры
SERVICE_KEYWORDS = {
    "ботокс": "botoks",
    "ботул": "botoks",
    "лицо": "botoks",       # "ботокс лицо"
    "губ": "filler",
    "контурн": "filler",
    "филлер": "filler",
    "гиалурон": "filler",
    "пилинг": "peeling",
    "чистк": "peeling",
    "скраб": "peeling",
    "биоревитализац": "biorevital",
    "мезотерап": "biorevital",
    "мезо": "biorevital",
}

REMINDERS = {
    "botoks": {
        "before": (
            "❗️ <b>Как подготовиться к ботоксу:</b>\n\n"
            "• За 24 часа исключите алкоголь (разжижает кровь, риск синяков выше)\n"
            "• За 2 недели прекратите приём антибиотиков (обсудите с врачом)\n"
            "• Приходите без макияжа на лице\n"
            "• Если есть аллергии или принимаете лекарства — сообщите заранее"
        ),
        "after": (
            "✅ <b>После ботокса — важно:</b>\n\n"
            "• Первые 4 часа не ложитесь горизонтально (препарат может сместиться)\n"
            "• 7 дней без бани, сауны и алкоголя (тепло и спирт ускоряют выведение)\n"
            "• Не трогайте места инъекций руками\n"
            "• Не наклоняйтесь надолго вниз\n"
            "• Эффект проявится через 3-7 дней\n\n"
            "Если появились вопросы — пишите! 💬"
        ),
    },
    "filler": {
        "before": (
            "❗️ <b>Как подготовиться к филлерам:</b>\n\n"
            "• За сутки исключите кофе, энергетики и алкоголь (повышают давление — сильный отёк)\n"
            "• За 3 дня прекратите приём Аспирина и Омега-3\n"
            "• Приходите без макияжа\n"
            "• Если есть аллергии — обязательно сообщите"
        ),
        "after": (
            "✅ <b>После филлеров — важно:</b>\n\n"
            "• 3 дня не пейте горячие напитки, не пейте через трубочку и не целуйтесь\n"
            "• 7 дней без бани и сауны (гиалуроновая кислота рассасывается от тепла)\n"
            "• Не трогайте обработанную зону руками\n"
            "• Спите на спине первые 2-3 ночи\n"
            "• Возможен отёк — это нормально, пройдёт за 2-3 дня\n\n"
            "Если появились вопросы — пишите! 💬"
        ),
    },
    "peeling": {
        "before": (
            "❗️ <b>Как подготовиться к пилингу/чистке:</b>\n\n"
            "• За 3 дня не используйте домашние скрабы\n"
            "• Не загорайте за 3 дня до процедуры\n"
            "• Не используйте ретинол за 5 дней\n"
            "• Приходите без макияжа"
        ),
        "after": (
            "✅ <b>После пилинга/чистки — важно:</b>\n\n"
            "• 24 часа не наносите макияж и тональный крем (поры открыты)\n"
            "• Обязательно наносите SPF перед выходом\n"
            "• Не трогайте лицо грязными руками\n"
            "• Не используйте активные средства 3-5 дней\n"
            "• Возможное шелушение — это нормально\n\n"
            "Если появились вопросы — пишите! 💬"
        ),
    },
    "biorevital": {
        "before": (
            "❗️ <b>Как подготовиться к биоревитализации:</b>\n\n"
            "• За сутки без алкоголя и кроверазжижающих препаратов\n"
            "• Не принимайте Аспирин и Омега-3 за 3 дня\n"
            "• Приходите без макияжа\n"
            "• Если есть аллергии — сообщите заранее"
        ),
        "after": (
            "✅ <b>После биоревитализации — важно:</b>\n\n"
            "• 24 часа не трогайте лицо руками и не наносите косметику\n"
            "• 3 дня без тяжёлого спорта и бани (пот вызывает воспаление папул)\n"
            "• Не трогайте папулы — они пройдут за 1-3 дня\n"
            "• Используйте SPF перед выходом\n\n"
            "Если появились вопросы — пишите! 💬"
        ),
    },
}

DEFAULT_REMINDER = {
    "before": (
        "❗️ <b>Подготовка к процедуре:</b>\n\n"
        "• Приходите без макияжа на обрабатываемой зоне\n"
        "• Сообщите об аллергиях и принимаемых лекарствах\n"
        "• Если есть вопросы — напишите нам заранее!"
    ),
    "after": (
        "✅ <b>После процедуры:</b>\n\n"
        "• Следуйте рекомендациям, которые обсудили на приёме\n"
        "• Если появились вопросы или беспокойства — пишите!\n"
        "• Мы на связи 💬"
    ),
}


def detect_service_type(service_name: str) -> str:
    """Определяет тип процедуры по ключевым словам в названии."""
    name_lower = service_name.lower()
    for keyword, service_type in SERVICE_KEYWORDS.items():
        if keyword in name_lower:
            return service_type
    return "default"


def get_before_reminder(service_name: str) -> str:
    """Возвращает текст памятки ДО процедуры."""
    service_type = detect_service_type(service_name)
    return REMINDERS.get(service_type, DEFAULT_REMINDER)["before"]


def get_after_reminder(service_name: str) -> str:
    """Возвращает текст памятки ПОСЛЕ процедуры."""
    service_type = detect_service_type(service_name)
    return REMINDERS.get(service_type, DEFAULT_REMINDER)["after"]
