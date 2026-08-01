"""
Auto-CRM: Автоматизированная воронка продаж и заботы о клиентах.

Сценарий:
  1. Клиент записывается → статус "Записан" в Google Sheets
  2. Админ подтверждает → статус "Подтвержден", уведомление клиенту
  3. Процедура завершена → обновляется "Последняя процедура", "Сумма", "Дата"
  4. Через 7 дней → follow-up: "Как дела после процедуры?"
  5. Через 3 месяца → re-engagement через Grok API: "Эффект может спадать, не хотите освежить?"
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func as sa_func, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from utils.helpers import now_salon
from database import Booking, Service, User

logger = logging.getLogger(__name__)


# =============================================================================
# СТАТУСЫ ДЛЯ GOOGLE SHEETS
# =============================================================================

STATUS_NEW = "Новый"
STATUS_BOOKED = "Записан"
STATUS_CONFIRMED = "Подтвержден"
STATUS_COMPLETED = "Выполнен"
STATUS_FOLLOWUP_SENT = "Фоллоу-ап"
STATUS_REENGAGED = "Прогрев 3 мес"
STATUS_CANCELLED = "Отменен"


def _get_status_label(booking: Booking, followup_sent: bool = False, reengaged: bool = False) -> str:
    """Определяет статус клиента для Google Sheets."""
    if booking.status == "cancelled":
        return STATUS_CANCELLED
    if booking.status == "completed":
        if reengaged:
            return STATUS_REENGAGED
        if followup_sent:
            return STATUS_FOLLOWUP_SENT
        return STATUS_COMPLETED
    if booking.status == "confirmed":
        return STATUS_CONFIRMED
    if booking.status == "pending":
        return STATUS_BOOKED
    return STATUS_NEW


# =============================================================================
# GROK: ГЕНЕРАЦИЯ ТЕКСТОВ
# =============================================================================

async def generate_followup_text(service_name: str, client_name: str) -> str:
    """Генерирует текст follow-up через 7 дней через Grok API."""
    try:
        from utils.grok import ask_grok
        prompt = (
            f"Ты — заботливый помощник косметолога Ашуры. "
            f"Клиент неделю назад сделал процедуру: {service_name}. "
            f"Напиши короткое (2-3 предложения) и теплое сообщение-напоминание. "
            f"Спросить как дела, всё ли хорошо после процедуры. "
            f"Тон: дружелюбный, не навязчивый. Без рекламы. На русском."
        )
        result = await ask_grok(
            history=[{"role": "user", "content": prompt}],
            system_prompt="Ты — заботливый помощник косметолога. Пиши кратко, тепло, по-русски."
        )
        if result and len(result) > 20:
            return result
    except Exception as e:
        logger.warning("Grok followup generation failed: %s", e)

    # Fallback
    return (
        f"Здравствуйте, {client_name}! "
        f"Прошла неделя после вашей процедуры ({service_name}). "
        f"Как вы себя чувствуете? Всё ли хорошо? 💫"
    )


async def generate_reengagement_text(
    service_name: str, client_name: str, days_since: int, next_visit_days: int
) -> str:
    """Генерирует текст 3-месячного re-engagement через Grok API."""
    try:
        from utils.grok import ask_grok
        prompt = (
            f"Ты — заботливый помощник косметолога Ашуры. "
            f"Клиент делал(а) {service_name} {days_since} дней назад. "
            f"Рекомендуемый интервал повторной процедуры: {next_visit_days} дней. "
            f"Напиши аккуратное, ненавязчивое сообщение (3-4 предложения). "
            f"Суть: эффект процедуры может постепенно спадать, "
            f"если захочет освежить результат — буду рада помочь. "
            f"НЕ рекламируй, НЕ дави. Тон: как подруга-косметолог. "
            f"Добавь мягкий призыв записаться через /start. На русском."
        )
        result = await ask_grok(
            history=[{"role": "user", "content": prompt}],
            system_prompt=(
                "Ты — профессиональный косметологический ассистент. "
                "Пиши как живой человек, не как робот. Кратко, тепло, без спама. "
                "Никогда не обещай конкретных результатов процедур."
            )
        )
        if result and len(result) > 30:
            return result
    except Exception as e:
        logger.warning("Grok reengagement generation failed: %s", e)

    # Fallback
    return (
        f"Здравствуйте, {client_name}! 💫\n\n"
        f"Прошло уже {days_since} дней с момента вашей процедуры ({service_name}). "
        f"Эффект от процедуры может постепенно спадать — это естественно.\n\n"
        f"Если захотите освежить результат или попробовать что-то новое — "
        f"буду рада помочь! Записаться можно через /start 🌸"
    )


async def generate_smart_suggestion(
    service_name: str, client_name: str, visit_count: int, total_spent: int
) -> str:
    """Генерирует умное предложение для админа на основе истории клиента."""
    try:
        from utils.grok import ask_grok
        prompt = (
            f"Ты — бизнес-ассистент косметолога. "
            f"Клиент. "
            f"Количество визитов: {visit_count}. "
            f"Общая сумма: {total_spent} руб. "
            f"Последняя процедура: {service_name}.\n\n"
            f"Предложи 2-3 конкретных действия для этого клиента:\n"
            f"1. Какую процедуру можно предложить следующей?\n"
            f"2. Есть ли смысл в персональной скидке?\n"
            f"3. Как лучше написать клиенту?\n\n"
            f"Отвечай кратко, конкретно, по делу. На русском."
        )
        result = await ask_grok(
            history=[{"role": "user", "content": prompt}],
            system_prompt="Ты — эксперт по удержанию клиентов в beauty-индустрии."
        )
        if result and len(result) > 50:
            return result
    except Exception as e:
        logger.warning("Grok suggestion generation failed: %s", e)

    # Fallback
    discount = 10 if visit_count >= 5 else 5 if visit_count >= 3 else 0
    suggestion = (
        f"Клиент {client_name}:\n"
        f"- Визитов: {visit_count}, потрачено: {total_spent} руб.\n"
        f"- Последняя: {service_name}\n"
    )
    if discount > 0:
        suggestion += f"- Рекомендация: предложить персональную скидку {discount}%\n"
    suggestion += "- Напомнить о себе через Telegram"
    return suggestion


# =============================================================================
# ОБНОВЛЕНИЕ GOOGLE SHEETS
# =============================================================================

async def update_sheet_status(telegram_id: int, updates: dict) -> None:
    """Обновляет конкретные колонки в Google Sheets для клиента."""
    try:
        from utils.google_sheets import _get_sheets_client
        gc, creds = _get_sheets_client()
        if not gc:
            return

        import os
        sheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
        if not sheet_id:
            return

        sh = gc.open_by_key(sheet_id)
        ws = sh.sheet1

        # Ищем строку клиента по TG ID
        try:
            cell = ws.find(str(telegram_id))
        except Exception:
            return

        if not cell:
            return

        row = cell.row

        # Обновляем колонки
        col_map = {
            "status": 10,       # J: Статус
            "bot_wrote": 11,    # K: Бот писал
            "notes": 12,        # L: Заметки
            "last_procedure": 5, # E: Последняя процедура
            "last_date": 6,     # F: Дата последней
            "last_amount": 7,   # G: Сумма последней
            "total_spent": 8,   # H: Всего потратил
            "next_visit": 9,    # I: Следующий визит
            "visits": 4,        # D: Визитов
        }

        for key, col_idx in col_map.items():
            if key in updates:
                col_letter = chr(64 + col_idx)  # A=1, B=2, ...
                ws.update(f"{col_letter}{row}", [[updates[key]]])

        logger.info("Sheet updated for TG %s: %s", telegram_id, list(updates.keys()))

    except Exception as e:
        logger.warning("Sheet update failed for TG %s: %s", telegram_id, e)


async def mark_sheet_dirty(user_id: int) -> None:
    """Помечает пользователя для синхронизации с Google Sheets."""
    try:
        from database import async_session
        async with async_session() as session:
            await session.execute(
                sa_update(User).where(User.id == user_id).values(sheets_dirty=True)
            )
            await session.commit()
    except Exception as e:
        logger.warning("mark_sheet_dirty failed: %s", e)


# =============================================================================
# ОБРАБОТКА СОБЫТИЙ (вызывается из handlers)
# =============================================================================

async def on_booking_created(booking: Booking, user: User) -> None:
    """Вызывается при создании записи."""
    await update_sheet_status(user.telegram_id, {
        "status": STATUS_BOOKED,
        "notes": f"Запись на {booking.preferred_date} {booking.preferred_time}",
    })
    await mark_sheet_dirty(user.id)


async def on_booking_confirmed(booking: Booking, user: User) -> None:
    """Вызывается при подтверждении записи админом."""
    service_name = booking.service.name if booking.service else "Услуга"
    await update_sheet_status(user.telegram_id, {
        "status": STATUS_CONFIRMED,
        "notes": f"Подтверждено: {booking.preferred_date} {booking.preferred_time}",
    })
    await mark_sheet_dirty(user.id)


async def on_booking_completed(booking: Booking, user: User, amount: int) -> None:
    """Вызывается при завершении процедуры."""
    service_name = booking.service.name if booking.service else "Услуга"
    today = now_salon().strftime("%d.%m.%Y")

    await update_sheet_status(user.telegram_id, {
        "status": STATUS_COMPLETED,
        "last_procedure": service_name,
        "last_date": today,
        "last_amount": str(amount),
        "notes": "",
    })
    await mark_sheet_dirty(user.id)


async def on_booking_cancelled(booking: Booking, user: User) -> None:
    """Вызывается при отмене записи."""
    await update_sheet_status(user.telegram_id, {
        "status": STATUS_CANCELLED,
        "notes": "Запись отменена",
    })
    await mark_sheet_dirty(user.id)


async def on_followup_sent(user: User, message_text: str) -> None:
    """Вызывается при отправке follow-up клиенту."""
    today = now_salon().strftime("%d.%m.%Y")
    await update_sheet_status(user.telegram_id, {
        "status": STATUS_FOLLOWUP_SENT,
        "bot_wrote": f"Follow-up {today}",
        "notes": message_text[:100],
    })


async def on_reengagement_sent(user: User, message_text: str) -> None:
    """Вызывается при отправке 3-месячного re-engagement."""
    today = now_salon().strftime("%d.%m.%Y")
    await update_sheet_status(user.telegram_id, {
        "status": STATUS_REENGAGED,
        "bot_wrote": f"Прогрев 3 мес {today}",
        "notes": message_text[:100],
    })


async def on_client_responded(user: User, response_text: str) -> None:
    """Вызывается когда клиент отвечает на follow-up/re-engagement."""
    today = now_salon().strftime("%d.%m.%Y")
    await update_sheet_status(user.telegram_id, {
        "notes": f"Ответ клиента ({today}): {response_text[:80]}",
    })


# =============================================================================
# SHEDULER JOB: 3-МЕСЯЧНЫЙ RE-ENGAGEMENT
# =============================================================================

async def send_reengagement_messages(bot, session: AsyncSession) -> int:
    """
    Отправляет re-engagement сообщения клиентам, которые не были 3+ месяца.
    Использует Grok API для генерации текстов.
    """
    from utils.helpers import now_salon, ACTIVE_BOOKING_STATUSES

    now = now_salon()
    three_months_ago = now - timedelta(days=90)

    # Ищем клиентов с completed визитами 90+ дней назад
    result = await session.execute(
        select(User, Booking, Service)
        .join(Booking, User.id == Booking.user_id)
        .join(Service, Booking.service_id == Service.id)
        .where(Booking.status == "completed")
        .where(Booking.completed_at <= three_months_ago)
        .where(User.pd_consent_at.isnot(None))
        .where(User.name.notlike("Удалён_%"))
    )
    rows = result.all()

    sent = 0
    for user, booking, service in rows:
        # Пропускаем если уже отправляли re-engagement недавно (менее60 дней назад)
        # followup_sent_at от7-дневного follow-up НЕ блокирует re-engagement
        if booking.followup_sent_at and (now - booking.followup_sent_at).days < 60:
            continue

        # Пропускаем если есть активная запись
        active_check = await session.execute(
            select(sa_func.count(Booking.id))
            .where(Booking.user_id == user.id)
            .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        )
        if (active_check.scalar() or 0) > 0:
            continue

        # Пропускаем если отключены напоминания
        if user.revisit_reminder_disabled:
            continue

        days_since = (now.date() - booking.completed_at.date()).days
        next_visit_days = service.revisit_days or 90

        # Генерируем текст через Grok
        text = await generate_reengagement_text(
            service.name, user.name, days_since, next_visit_days
        )

        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
            )
            # Помечаем что отправили
            booking.followup_sent_at = now
            await session.flush()

            # Обновляем Google Sheets
            await on_reengagement_sent(user, text)

            sent += 1
            logger.info("Re-engagement sent to %s (%s, %d days)", user.telegram_id, service.name, days_since)

        except Exception as e:
            logger.warning("Re-engagement failed for %s: %s", user.telegram_id, e)

    return sent


# =============================================================================
# SHEDULER JOB: FOLLOW-UP С GROK
# =============================================================================

async def send_enhanced_followups(bot, session: AsyncSession) -> int:
    """
    Улучшенный follow-up через 7 дней с Grok-генерацией текста.
    Заменяет стандартный _send_post_procedure_followups.
    """
    from utils.helpers import now_salon

    now = now_salon()
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.user), joinedload(Booking.service))
        .where(Booking.status == "completed")
        .where(Booking.completed_at.isnot(None))
        .where(Booking.followup_sent_at.is_(None))
    )
    bookings = result.scalars().unique().all()

    sent = 0
    for booking in bookings:
        elapsed = now - booking.completed_at
        if elapsed < timedelta(days=7):
            continue
        if elapsed > timedelta(days=14):
            booking.followup_sent_at = now
            await session.flush()
            continue

        user = booking.user
        if not user:
            continue

        service_name = booking.service.name if booking.service else "процедура"

        # Генерируем текст через Grok
        text = await generate_followup_text(service_name, user.name)

        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            from keyboards import post_procedure_feedback_keyboard

            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=post_procedure_feedback_keyboard(booking.id),
            )
            booking.followup_sent_at = now
            await session.flush()

            # Обновляем Google Sheets
            await on_followup_sent(user, text)

            sent += 1
            logger.info("Follow-up sent to %s (%s)", user.telegram_id, service_name)

        except Exception as e:
            logger.warning("Follow-up failed for %s: %s", user.telegram_id, e)

    return sent
