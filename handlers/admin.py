"""
Админские handlers (только для Ашуры):
- /admin — главное меню админки
- Заявки на запись (принять/отклонить/выполнить)
- Статистика
- Модерация отзывов
- Управление бонусами
- Рассылка всем клиентам
- Управление услугами
- Управление FAQ
- Ответ клиенту (/reply)
"""

import asyncio
import logging
from datetime import datetime, timezone
from html import escape as html_escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from config import Config
from database import (
    Booking,
    BonusTransaction,
    FAQ,
    Review,
    Service,
    User,
)
from keyboards import (
    admin_accept_choice_keyboard,
    admin_bonus_amount_keyboard,
    admin_bonus_clients_keyboard,
    admin_bonus_revoke_clients_keyboard,
    admin_bonus_revoke_tx_keyboard,
    admin_bonuses_menu_keyboard,
    admin_visit_bonus_keyboard,
    admin_booking_completed_keyboard,
    admin_booking_confirmed_keyboard,
    admin_booking_keyboard,
    admin_bookings_filter_keyboard,
    admin_empty_confirmed_keyboard,
    admin_broadcast_confirm_keyboard,
    admin_main_keyboard,
    admin_review_moderation_keyboard,
    admin_stats_period_keyboard,
    back_to_main_keyboard,
)
from utils.audit import log_admin_action
from utils.helpers import (
    add_bonus_transaction,
    parse_callback_int,
    format_booking_services_line,
    format_phone,
    format_price,
    format_stats,
    get_stats,
    grant_confirmation_bonus,
    notify_client_booking_cancelled,
    notify_client_booking_confirmed,
    send_message_to_owner,
    validate_phone,
)
from utils.states import (
    AdminAcceptState,
    AdminBonusGrantState,
    AdminBonusRevokeState,
    AdminCompleteState,
    AdminFaqAddState,
    AdminRejectState,
    AdminReplyState,
    AdminServiceAddState,
    BroadcastState,
)

logger = logging.getLogger(__name__)


# =============================================================================
# MIDDLEWARE: проверка прав админа на уровне роутера
# =============================================================================

class AdminOnlyMiddleware:
    """Middleware that blocks non-admin users from all handlers on this router."""
    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        if not user:
            # For callback queries, from_user is on the event
            inner = getattr(event, 'event', None)
            if inner:
                user = getattr(inner, 'from_user', None)
        if not user:
            return None
        
        if user.id != Config.ADMIN_ID:
            if hasattr(event, 'answer'):
                try:
                    await event.answer('⛔ Нет доступа!', show_alert=True)
                except Exception:
                    pass
            return None
        
        return await handler(event, data)


router = Router()
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

# =============================================================================
# КОНСТАНТЫ СТАТУСОВ
# =============================================================================

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

STATUS_NAMES = {
    STATUS_PENDING: "🆕 Новые",
    STATUS_CONFIRMED: "✅ Подтверждённые",
    STATUS_COMPLETED: "🏁 Выполненные",
    STATUS_CANCELLED: "❌ Отменённые",
}


# =============================================================================
# ПРОВЕРКА ПРАВ АДМИНА (оставлена для PrivacyConsentMiddleware)
# =============================================================================

async def is_admin(telegram_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return telegram_id == Config.ADMIN_ID


async def _booking_status_counts(session: AsyncSession) -> dict[str, int]:
    """Количество заявок по каждому статусу."""
    result = await session.execute(
        select(Booking.status, func.count(Booking.id)).group_by(Booking.status)
    )
    return {status: count for status, count in result.all()}


def _format_booking_when(booking: Booking) -> str:
    """Дата/время записи для карточки админки."""
    if booking.preferred_date:
        when = booking.preferred_date
        if booking.preferred_time:
            when = f"{when}, {booking.preferred_time}"
        return when
    return "не назначена"


def _format_admin_booking_card(
    booking: Booking,
    *,
    status_label: str,
) -> str:
    """Текст карточки заявки в списке админки."""
    user = booking.user
    service_name = format_booking_services_line(booking)
    when = _format_booking_when(booking)

    anam_text = ""
    if booking.anamnesis_json:
        from utils.helpers import format_anamnesis
        anam_text = f"\n{format_anamnesis(booking.anamnesis_json)}\n"

    bonus_text = ""
    if booking.bonus_used and booking.bonus_used > 0:
        bonus_text = f"\n🎁 Скидка бонусами: -{format_price(booking.bonus_used)}\n"

    confirmed_text = ""
    if booking.confirmed_at:
        confirmed_text = (
            f"\n✅ Подтверждена админом: "
            f"{booking.confirmed_at.strftime('%d.%m %H:%M')}"
        )

    from utils.privacy import format_pd_consent_admin_line

    return (
        f"🆔 <b>#{booking.id}</b> | {status_label}\n"
        f"👤 <b>{html_escape(user.name)}</b> | 📱 {format_phone(user.phone)}\n"
        f"{format_pd_consent_admin_line(user)}"
        f"💅 <b>{service_name}</b>\n"
        f"📅 Когда: {when}"
        f"{confirmed_text}\n"
        f"📝 {html_escape(booking.notes) if booking.notes else '—'}\n"
        f"{anam_text}"
        f"{bonus_text}"
        f"📅 Создана: {booking.created_at.strftime('%d.%m %H:%M')}"
    )


def _bonus_granted_line(bonus_granted: int) -> str:
    """Строка для админа о начисленных бонусах при подтверждении."""
    if bonus_granted <= 0:
        return ""
    return (
        f"🎁 Клиентке начислено +{bonus_granted} бонусов "
        f"({Config.BONUS_PERCENT}% скидка).\n"
    )


async def _check_slot_collision(
    session: AsyncSession, booking: Booking,
) -> str:
    """Проверяет, нет ли другой confirmed-записи на то же дату/время. Возвращает предупреждение."""
    if not booking.preferred_date:
        return ""

    query = (
        select(Booking)
        .options(joinedload(Booking.user))
        .where(
            Booking.status == STATUS_CONFIRMED,
            Booking.id != booking.id,
            Booking.preferred_date == booking.preferred_date,
        )
    )
    result = await session.execute(query)
    others = result.scalars().unique().all()

    for other in others:
        if other.preferred_date == booking.preferred_date:
            time_match = (
                not booking.preferred_time
                or not other.preferred_time
                or booking.preferred_time == other.preferred_time
            )
            if time_match:
                name = html_escape(other.user.name) if other.user else "?"
                return (
                    f"\n⚠️ <b>Коллизия:</b> на {booking.preferred_date}"
                    f"{f', {booking.preferred_time}' if booking.preferred_time else ''}"
                    f" уже записана {name} (#{other.id})"
                )
    return ""


async def _confirm_booking(
    session: AsyncSession,
    bot: Bot,
    booking: Booking,
    *,
    when_label: str,
    admin_id: int,
) -> tuple[bool, int]:
    """
    Переводит заявку в confirmed, начисляет 3% бонусов и уведомляет клиента.
    Возвращает (доставлено_уведомление, сумма_бонусов).
    """
    from utils.helpers import now_salon
    from sqlalchemy import update as sa_update
    # Атомарное обновление статуса — защита от double-confirm
    result = await session.execute(
        sa_update(Booking)
        .where(Booking.id == booking.id, Booking.status == STATUS_PENDING)
        .values(status=STATUS_CONFIRMED, confirmed_at=now_salon())
    )
    if result.rowcount == 0:
        return False, 0  # уже подтверждена другим запросом
    # Перезагружаем booking вместе с user relationship
    from sqlalchemy.orm import joinedload
    fresh = await session.execute(
        select(Booking).options(joinedload(Booking.user)).where(Booking.id == booking.id)
    )
    booking = fresh.scalar_one()

    bonus_granted = await grant_confirmation_bonus(session, booking)

    client_tg_id = booking.user.telegram_id
    service_name = format_booking_services_line(booking)
    await session.flush()

    notified = True
    try:
        await notify_client_booking_confirmed(
            bot,
            client_tg_id,
            booking,
            service_name=service_name,
            bonus_granted=bonus_granted,
        )
    except Exception as e:
        logger.error("Ошибка уведомления клиента о подтверждении: %s", e)
        notified = False

    logger.info(
        "Заявка #%s подтверждена, назначено: %s (уведомление: %s, бонусы: +%s)",
        booking.id, when_label, "да" if notified else "нет", bonus_granted,
    )
    log_admin_action(admin_id, 'accept_booking', f'#{booking.id}', when_label)
    return notified, bonus_granted


async def _fetch_booking(
    session: AsyncSession, booking_id: int,
) -> Booking | None:
    """
    Загружает заявку с user и service одним запросом.
    Без joinedload позже срабатывает lazy-load → greenlet_spawn error.
    """
    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.user), joinedload(Booking.service))
        .where(Booking.id == booking_id)
    )
    return result.scalar_one_or_none()


# =============================================================================
# /ADMIN — Главное меню админки
# =============================================================================

@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Открывает главное меню администратора."""
    await message.answer(
        f"🔐 <b>Админ-панель {Config.SALON_NAME}</b>\n\n"
        f"Выберите раздел:",
        reply_markup=admin_main_keyboard(),
        parse_mode="HTML",
    )


# =============================================================================
# ВОЗВРАТ В АДМИНКУ
# =============================================================================

@router.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в главное меню админки."""
    await state.clear()
    await callback.message.answer(
        f"🔐 <b>Админ-панель</b>\n\nВыберите раздел:",
        reply_markup=admin_main_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# =============================================================================
# ЗАЯВКИ НА ЗАПИСЬ
# =============================================================================

@router.callback_query(F.data == "admin_bookings")
async def admin_bookings(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Показывает фильтры заявок."""
    counts = await _booking_status_counts(session)
    await callback.message.answer(
        f"📋 <b>Заявки на запись</b>\n\n"
        f"🆕 Новые — ожидают вашего «Принять».\n"
        f"✅ Подтверждённые — вы уже согласовали дату с клиентом.\n\n"
        f"Выберите фильтр:",
        reply_markup=admin_bookings_filter_keyboard(counts),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("book_filter_"))
async def admin_bookings_list(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Показывает список заявок по статусу."""
    status = callback.data.removeprefix("book_filter_")
    status_label = STATUS_NAMES.get(status, status)
    counts = await _booking_status_counts(session)

    await callback.answer()

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.user), joinedload(Booking.service))
        .where(Booking.status == status)
        .order_by(Booking.created_at.desc())
        .limit(20)
    )
    bookings = result.scalars().unique().all()

    if not bookings:
        empty_hint = ""
        if status == STATUS_CONFIRMED:
            pending_n = counts.get(STATUS_PENDING, 0)
            empty_hint = (
                "\n\n<i>Подтверждённые появляются после кнопки «Принять» "
                "в уведомлении о новой записи.</i>"
            )
            if pending_n:
                empty_hint += (
                    f"\n\nСейчас есть <b>{pending_n}</b> новых — "
                    f"откройте их и нажмите «Принять»."
                )
            markup = admin_empty_confirmed_keyboard(counts)
        else:
            markup = admin_bookings_filter_keyboard(counts)
        await callback.message.answer(
            f"{status_label}: пусто{empty_hint}",
            reply_markup=markup,
            parse_mode="HTML",
        )
        return

    await callback.message.answer(
        f"{status_label}: <b>{len(bookings)}</b>",
        parse_mode="HTML",
    )

    for b in bookings:
        text = _format_admin_booking_card(b, status_label=status_label)

        if status == STATUS_PENDING:
            markup = admin_booking_keyboard(b.id)
        elif status == STATUS_CONFIRMED:
            markup = admin_booking_confirmed_keyboard(b.id)
        elif status == STATUS_COMPLETED:
            markup = admin_booking_completed_keyboard(b.id)
        else:
            markup = None

        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")

    await callback.message.answer(
        "Выберите другой фильтр:",
        reply_markup=admin_bookings_filter_keyboard(counts),
    )


# =============================================================================
# ПРИНЯТЬ ЗАЯВКУ — Callback -> FSM
# =============================================================================

@router.callback_query(F.data.regexp(r"^admin_accept_quick_(\d+)$"))
async def admin_accept_quick(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Подтверждает заявку на дату, которую выбрал клиент."""
    await callback.answer()
    booking_id = parse_callback_int(callback.data, "admin_accept_quick_")
    if booking_id is None:
        await callback.message.answer("⚠️ Ошибка данных.")
        return
    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await callback.message.answer("⚠️ Заявка не найдена.")
        return

    if booking.status != STATUS_PENDING:
        await callback.message.answer(
            f"⚠️ Заявка #{booking_id} уже в статусе «{booking.status}»."
        )
        return

    when_label = _format_booking_when(booking)
    collision = await _check_slot_collision(session, booking)

    if collision:
        # Есть коллизия — спрашиваем подтверждение
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="✅ Всё равно подтвердить",
                callback_data=f"admin_force_accept_{booking_id}",
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back"),
        )
        await callback.message.answer(
            f"⚠️ <b>Коллизия слота</b> — заявка #{booking_id}\n\n"
            f"📅 {when_label}{collision}\n\n"
            f"Подтвердить всё равно?",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        return

    notified, bonus_granted = await _confirm_booking(
        session, callback.bot, booking, when_label=when_label,
        admin_id=callback.from_user.id,
    )

    notify_line = (
        "👤 Клиент уведомлён.\n"
        if notified
        else "⚠️ Клиенту не удалось отправить уведомление — свяжитесь вручную.\n"
    )
    await callback.message.answer(
        f"✅ <b>Заявка #{booking_id} подтверждена!</b>\n\n"
        f"📅 Назначено: {when_label}\n"
        f"{notify_line}"
        f"{_bonus_granted_line(bonus_granted)}\n"
        f"Теперь она в разделе «Подтверждённые».",
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin_force_accept_(\d+)$"))
async def admin_force_accept(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Подтверждение заявки при коллизии (после предупреждения)."""
    booking_id = parse_callback_int(callback.data, "admin_force_accept_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if booking.status != STATUS_PENDING:
        await callback.answer(f"Заявка уже {booking.status}.", show_alert=True)
        return

    await callback.answer()
    when_label = _format_booking_when(booking)
    notified, bonus_granted = await _confirm_booking(
        session, callback.bot, booking, when_label=when_label,
        admin_id=callback.from_user.id,
    )

    notify_line = (
        "👤 Клиент уведомлён.\n"
        if notified
        else "⚠️ Уведомление не доставлено — свяжитесь вручную.\n"
    )
    await callback.message.answer(
        f"✅ <b>Заявка #{booking_id} подтверждена!</b>\n\n"
        f"📅 Назначено: {when_label}\n"
        f"{notify_line}"
        f"{_bonus_granted_line(bonus_granted)}\n"
        f"Теперь она в разделе «Подтверждённые».",
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^admin_accept_custom_(\d+)$"))
async def admin_accept_custom(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    """Указать другую дату приёма — переводит в FSM."""
    booking_id = parse_callback_int(callback.data, "admin_accept_custom_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    await state.update_data(accept_booking_id=booking_id)

    await callback.message.answer(
        f"✅ <b>Принятие заявки #{booking_id}</b>\n\n"
        f"Напишите дату и время приёма:\n"
        f"Формат: <b>ДД.ММ.ГГГГ</b> или <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\n"
        f"Примеры:\n"
        f"• 15.07.2026\n"
        f"• 15.07.2026 14:00",
        parse_mode="HTML",
    )
    await state.set_state(AdminAcceptState.waiting_datetime)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_accept_(\d+)$"))
async def admin_accept_booking(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Начинает принятие заявки — быстро на дату клиента или ввод вручную."""
    # Защита от коллизии: пропускаем если callback длиннее (quick_, custom_, force_accept_)
    data = callback.data
    if data.startswith(("admin_accept_quick_", "admin_accept_custom_", "admin_force_accept_")):
        return

    booking_id = int(data.removeprefix("admin_accept_"))
    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if booking.status != STATUS_PENDING:
        await callback.answer(
            f"Заявка уже в статусе: {booking.status}", show_alert=True,
        )
        return

    await callback.answer()

    if booking.preferred_date:
        when = _format_booking_when(booking)
        await callback.message.answer(
            f"✅ <b>Принятие заявки #{booking_id}</b>\n\n"
            f"Клиент просил: <b>{when}</b>\n\n"
            f"Подтвердить на эту дату или указать другую?",
            reply_markup=admin_accept_choice_keyboard(booking_id),
            parse_mode="HTML",
        )
        return

    await state.update_data(accept_booking_id=booking_id)
    await callback.message.answer(
        f"✅ <b>Принятие заявки #{booking_id}</b>\n\n"
        f"Напишите дату и время приёма:\n"
        f"Формат: <b>ДД.ММ.ГГГГ</b> или <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\n"
        f"Примеры:\n"
        f"• 15.07.2026\n"
        f"• 15.07.2026 14:00",
        parse_mode="HTML",
    )
    await state.set_state(AdminAcceptState.waiting_datetime)


@router.message(AdminAcceptState.waiting_datetime, F.text)
async def process_accept_datetime(
    msg: Message, session: AsyncSession, state: FSMContext,
) -> None:
    """FSM-хендлер: получает дату/время и подтверждает заявку."""
    data = await state.get_data()
    booking_id = data.get("accept_booking_id")
    if not booking_id:
        await msg.answer("⚠️ Ошибка: не найдена заявка. Начните заново.")
        await state.clear()
        return

    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await msg.answer("⚠️ Заявка не найдена.")
        await state.clear()
        return

    if booking.status != STATUS_PENDING:
        await msg.answer(f"⚠️ Заявка #{booking_id} уже в статусе «{booking.status}». Нельзя подтвердить.")
        await state.clear()
        return

    datetime_str = msg.text.strip()

    # Строгая валидация: только ДД.ММ.ГГГГ или ДД.ММ.ГГГГ ЧЧ:ММ
    import re
    match = re.fullmatch(
        r"(\d{1,2}\.\d{1,2}\.\d{4})(?:\s+(\d{1,2}:\d{2}))?",
        datetime_str,
    )
    if not match:
        await msg.answer(
            "⚠️ Нужен формат: <b>ДД.ММ.ГГГГ</b> или <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\n"
            "Примеры:\n"
            "• 15.07.2026\n"
            "• 15.07.2026 14:00",
            parse_mode="HTML",
        )
        return

    date_part = match.group(1)
    time_part = match.group(2) or ""

    # Нормализация: 5.7.2026 → 05.07.2026
    from datetime import datetime as dt
    try:
        parsed = dt.strptime(date_part, "%d.%m.%Y")
        date_part = parsed.strftime("%d.%m.%Y")
    except ValueError:
        await msg.answer("⚠️ Некорректная дата. Проверьте число и месяц.")
        return

    booking.preferred_date = date_part
    booking.preferred_time = time_part

    collision = await _check_slot_collision(session, booking)
    notified, bonus_granted = await _confirm_booking(
        session, msg.bot, booking, when_label=datetime_str,
        admin_id=msg.from_user.id,
    )
    notify_line = (
        "👤 Клиент уведомлён.\n"
        if notified
        else "⚠️ Уведомление клиенту не доставлено — свяжитесь вручную.\n"
    )

    await msg.answer(
        f"✅ <b>Заявка #{booking_id} принята!</b>\n\n"
        f"📅 Назначено: {datetime_str}\n"
        f"{notify_line}"
        f"{_bonus_granted_line(bonus_granted)}{collision}\n"
        f"Теперь она в разделе «Подтверждённые».",
        parse_mode="HTML",
    )
    await state.clear()


# =============================================================================
# ОТКЛОНИТЬ ЗАЯВКУ
# =============================================================================

@router.callback_query(F.data.startswith("admin_reject_"))
async def admin_reject_booking(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    """Запрашивает причину отклонения — затем уведомит клиента."""
    booking_id = parse_callback_int(callback.data, "admin_reject_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    await state.update_data(reject_booking_id=booking_id)

    await callback.message.answer(
        f"❌ <b>Отклонение заявки #{booking_id}</b>\n\n"
        f"Напишите причину для клиента\n"
        f"(или «-» — без причины):",
        parse_mode="HTML",
    )
    await state.set_state(AdminRejectState.waiting_reason)
    await callback.answer()


@router.message(AdminRejectState.waiting_reason, F.text)
async def process_reject_reason(
    msg: Message, session: AsyncSession, state: FSMContext,
) -> None:
    """Отклоняет заявку и отправляет клиенту уведомление с причиной."""
    data = await state.get_data()
    booking_id = data.get("reject_booking_id")
    if not booking_id:
        await msg.answer("⚠️ Заявка не найдена. Начните заново.")
        await state.clear()
        return

    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await msg.answer("⚠️ Заявка не найдена.")
        await state.clear()
        return

    # Только pending/confirmed/completed можно отклонить
    if booking.status not in (STATUS_PENDING, STATUS_CONFIRMED, STATUS_COMPLETED):
        await msg.answer(f"⚠️ Заявка #{booking_id} уже в статусе: {booking.status}. Нельзя отклонить.")
        await state.clear()
        return

    reason = html_escape(msg.text.strip())
    admin_id = msg.from_user.id
    # Атомарный reject — защита от race condition
    from sqlalchemy import update as sa_update
    result = await session.execute(
        sa_update(Booking)
        .where(Booking.id == booking.id, Booking.status.in_((STATUS_PENDING, STATUS_CONFIRMED, STATUS_COMPLETED)))
        .values(status=STATUS_CANCELLED)
    )
    if result.rowcount == 0:
        await msg.answer(f"⚠️ Заявка #{booking_id} уже обработана.")
        await state.clear()
        return
    await session.refresh(booking)

    # Читаем до await — relationship уже подгружен joinedload в _fetch_booking
    user = booking.user
    client_tg_id = user.telegram_id
    service_name = format_booking_services_line(booking)
    bonus_refunded = 0
    bonus_revoked = 0

    # Возврат бонусов, которые клиент потратил (bonus_used) — атомарно
    if booking.bonus_used and booking.bonus_used > 0:
        bonus_refunded = booking.bonus_used
        from sqlalchemy import update as sa_update
        await session.execute(
            sa_update(User)
            .where(User.id == user.id)
            .values(bonus_balance=User.bonus_balance + bonus_refunded)
        )
        await session.refresh(user)
        booking.bonus_used = 0  # Обнуляем чтобы нельзя было вернуть повторно
        await add_bonus_transaction(
            session,
            user.id,
            bonus_refunded,
            f"Возврат бонусов при отклонении записи #{booking_id}",
            booking_id=booking_id,
        )

    # Отзыв бонуса, начисленного при подтверждении (+3%) — атомарно
    confirmation_tx = await session.execute(
        select(BonusTransaction).where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.amount > 0,
            BonusTransaction.description.contains("при подтверждении"),
        )
    )
    conf_tx = confirmation_tx.scalar_one_or_none()
    if conf_tx and conf_tx.amount > 0:
        bonus_revoked = conf_tx.amount
        from sqlalchemy import update as sa_update
        await session.execute(
            sa_update(User)
            .where(User.id == user.id)
            .values(bonus_balance=func.max(0, User.bonus_balance - bonus_revoked))
        )
        await session.refresh(user)
        await add_bonus_transaction(
            session,
            user.id,
            -bonus_revoked,
            f"Отзыв бонуса при отмене записи #{booking_id}",
            booking_id=booking_id,
        )

    # Отзыв бонуса за визит (начисление при завершении) — атомарно
    visit_tx = await session.execute(
        select(BonusTransaction).where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.amount > 0,
            BonusTransaction.description.contains("за выполненную запись"),
        )
    )
    visit_tx_row = visit_tx.scalar_one_or_none()
    if visit_tx_row and visit_tx_row.amount > 0:
        visit_bonus_revoked = visit_tx_row.amount
        from sqlalchemy import update as sa_update
        await session.execute(
            sa_update(User)
            .where(User.id == user.id)
            .values(bonus_balance=func.max(0, User.bonus_balance - visit_bonus_revoked))
        )
        await session.refresh(user)
        await add_bonus_transaction(
            session,
            user.id,
            -visit_bonus_revoked,
            f"Отзыв бонуса визита при отмене записи #{booking_id}",
            booking_id=booking_id,
        )
        bonus_revoked += visit_bonus_revoked

    await session.flush()

    try:
        await notify_client_booking_cancelled(
            msg.bot,
            client_tg_id,
            service_name=service_name,
            reason=reason,
        )
    except Exception as e:
        logger.error("Ошибка уведомления об отклонении: %s", e)
        await msg.answer(
            f"❌ Заявка #{booking_id} отклонена, но уведомление клиенту "
            f"не доставлено: {e}"
        )
        await state.clear()
        return

    log_admin_action(msg.from_user.id, 'reject_booking', f'#{booking_id}', reason)

    refund_line = ""
    if bonus_refunded > 0:
        refund_line += f"\n🎁 Бонусы возвращены клиенту: +{bonus_refunded}"
    if bonus_revoked > 0:
        refund_line += f"\n🎁 Бонус подтверждения отозван: -{bonus_revoked}"
    await msg.answer(
        f"❌ Заявка #{booking_id} отклонена. Клиент уведомлён.{refund_line}"
    )
    await state.clear()


# =============================================================================
# ОТМЕТИТЬ ВЫПОЛНЕННОЙ — Callback -> FSM
# =============================================================================

@router.callback_query(F.data.startswith("admin_done_"))
async def admin_done_booking(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    """Начинает процесс завершения заявки — переводит в FSM."""
    booking_id = parse_callback_int(callback.data, "admin_done_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    await state.update_data(done_booking_id=booking_id)

    await callback.message.answer(
        f"🏁 <b>Заявка #{booking_id} — выполнена</b>\n\n"
        f"Введите итоговую сумму (число в рублях):\n"
        f"(например: 15000)",
        parse_mode="HTML",
    )
    await state.set_state(AdminCompleteState.waiting_amount)
    await callback.answer()


@router.message(AdminCompleteState.waiting_amount, F.text)
async def process_done_amount(
    msg: Message, session: AsyncSession, state: FSMContext,
) -> None:
    """FSM-хендлер: получает сумму, завершает заявку, предлагает начислить бонусы."""
    data = await state.get_data()
    booking_id = data.get("done_booking_id")
    if not booking_id:
        await msg.answer("⚠️ Ошибка: не найдена заявка. Начните заново.")
        await state.clear()
        return

    try:
        amount = int(msg.text.strip())
    except ValueError:
        await msg.answer("⚠️ Введите число (сумму в рублях):")
        return

    if amount <= 0:
        await msg.answer("⚠️ Сумма должна быть больше нуля.")
        return

    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await msg.answer("⚠️ Заявка не найдена.")
        await state.clear()
        return

    if booking.status != STATUS_CONFIRMED:
        await msg.answer(
            f"⚠️ Заявка #{booking_id} в статусе «{booking.status}». "
            f"Завершить можно только подтверждённую запись."
        )
        await state.clear()
        return

    from utils.helpers import now_salon
    # Атомарный complete — защита от race condition
    from sqlalchemy import update as sa_update
    result = await session.execute(
        sa_update(Booking)
        .where(Booking.id == booking.id, Booking.status == STATUS_CONFIRMED)
        .values(status=STATUS_COMPLETED, completed_at=now_salon(), total_amount=amount)
    )
    if result.rowcount == 0:
        await msg.answer(f"⚠️ Заявка #{booking_id} уже обработана.")
        await state.clear()
        return
    await session.refresh(booking)

    log_admin_action(msg.from_user.id, 'complete_booking', f'#{booking_id}', f'amount={amount}')

    user = booking.user
    service_name = format_booking_services_line(booking)

    await msg.answer(
        f"✅ <b>Заявка #{booking_id} выполнена!</b>\n\n"
        f"💅 {service_name}\n"
        f"💰 Сумма: {format_price(amount)}\n"
        f"👤 Клиент: {html_escape(user.name)}",
        parse_mode="HTML",
    )

    # Предлагаем начислить бонусы за визит (показываем реальную сумму бонуса, а не цену)
    suggested = _suggested_bonus_for_amount(amount)
    await msg.answer(
        "🎁 Начислить бонусы за визит?",
        reply_markup=admin_visit_bonus_keyboard(booking_id, suggested),
    )
    await state.clear()


# =============================================================================
# СТАТИСТИКА
# =============================================================================

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery) -> None:
    """Показывает выбор периода статистики."""
    await callback.message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Выберите период:",
        reply_markup=admin_stats_period_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stats_"))
async def admin_stats_period(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Показывает статистику за выбранный период."""
    period = callback.data.replace("stats_", "")
    stats = await get_stats(session, period)
    text = format_stats(stats)

    await callback.message.answer(
        text,
        reply_markup=admin_stats_period_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# =============================================================================
# КАЛЕНДАРЬ СЕГОДНЯ / ЗАВТРА
# =============================================================================

@router.callback_query(F.data == "admin_calendar")
async def admin_calendar(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает confirmed-записи на сегодня и завтра."""
    from datetime import timedelta
    from utils.helpers import now_salon

    today = now_salon().date()
    tomorrow = today + timedelta(days=1)
    today_str = today.strftime("%d.%m.%Y")
    tomorrow_str = tomorrow.strftime("%d.%m.%Y")

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.user), joinedload(Booking.service))
        .where(Booking.status == STATUS_CONFIRMED)
        .order_by(Booking.preferred_date, Booking.preferred_time)
    )
    bookings = result.scalars().unique().all()

    today_bookings = [b for b in bookings if b.preferred_date == today_str]
    tomorrow_bookings = [b for b in bookings if b.preferred_date == tomorrow_str]

    lines = [f"📅 <b>Календарь</b>\n"]

    lines.append(f"\n<b>Сегодня ({today_str}):</b>")
    if today_bookings:
        for b in today_bookings:
            time = b.preferred_time or "??:??"
            svc = format_booking_services_line(b)
            name = html_escape(b.user.name) if b.user else "?"
            lines.append(f"  ⏰ {time} — {name} ({svc})")
    else:
        lines.append("  <i>пусто</i>")

    lines.append(f"\n<b>Завтра ({tomorrow_str}):</b>")
    if tomorrow_bookings:
        for b in tomorrow_bookings:
            time = b.preferred_time or "??:??"
            svc = format_booking_services_line(b)
            name = html_escape(b.user.name) if b.user else "?"
            lines.append(f"  ⏰ {time} — {name} ({svc})")
    else:
        lines.append("  <i>пусто</i>")

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=admin_main_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# =============================================================================
# МОДЕРАЦИЯ ОТЗЫВОВ
# =============================================================================

@router.callback_query(F.data == "admin_reviews")
async def admin_reviews(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает отзывы на модерацию."""
    result = await session.execute(
        select(Review)
        .options(joinedload(Review.user))
        .where(Review.is_published == False)
        .order_by(Review.created_at.desc())
        .limit(20)
    )
    reviews = result.scalars().unique().all()

    if not reviews:
        await callback.message.answer(
            f"★ <b>Отзывы на модерацию</b>\n\n"
            f"<i>Все отзывы обработаны!</i>",
            reply_markup=admin_main_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    for r in reviews:
        user = r.user
        stars = "★" * r.rating
        text = (
            f"★ <b>Отзыв на модерацию</b>\n\n"
            f"👤 <b>{html_escape(user.name)}</b>\n"
            f"{stars}\n"
            f"<i>{html_escape(r.text) if r.text else 'Без текста'}</i>\n\n"
            f"📅 {r.created_at.strftime('%d.%m.%Y %H:%M')}"
        )
        await callback.message.answer(
            text,
            reply_markup=admin_review_moderation_keyboard(r.id),
            parse_mode="HTML",
        )

    await callback.answer()


@router.callback_query(F.data.startswith("rev_pub_"))
async def admin_publish_review(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Публикует отзыв."""
    review_id = parse_callback_int(callback.data, "rev_pub_")
    if review_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    result = await session.execute(
        select(Review).where(Review.id == review_id)
    )
    review = result.scalar_one_or_none()
    if review:
        review.is_published = True
        await session.flush()

    await callback.message.answer(f"✅ Отзыв #{review_id} опубликован!")
    await callback.answer()


@router.callback_query(F.data.startswith("rev_rej_"))
async def admin_reject_review(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Отклоняет отзыв (удаляет)."""
    review_id = parse_callback_int(callback.data, "rev_rej_")
    if review_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    result = await session.execute(
        select(Review).where(Review.id == review_id)
    )
    review = result.scalar_one_or_none()
    if review:
        await session.delete(review)

    await callback.message.answer(f"❌ Отзыв #{review_id} удалён.")
    await callback.answer()


# =============================================================================
# РАССЫЛКА — Асинхронная с задержкой
# =============================================================================

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало создания рассылки."""
    await callback.message.answer(
        f"📢 <b>Рассылка</b>\n\n"
        f"Выберите аудиторию:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="👥 Всем клиентам", callback_data="bc_seg_all")],
                [InlineKeyboardButton(text="📋 С активной записью", callback_data="bc_seg_active")],
                [InlineKeyboardButton(text="💤 Без записей", callback_data="bc_seg_inactive")],
                [InlineKeyboardButton(text="🎁 С бонусами > 0", callback_data="bc_seg_bonuses")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
            ]
        ),
        parse_mode="HTML",
    )
    await state.set_state(BroadcastState.waiting_segment)
    await callback.answer()


@router.callback_query(BroadcastState.waiting_segment, F.data.startswith("bc_seg_"))
async def broadcast_select_segment(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор сегмента → переход к вводу текста."""
    segment = callback.data.replace("bc_seg_", "")
    await state.update_data(broadcast_segment=segment)

    segment_names = {
        "all": "всем клиентам",
        "active": "с активной записью",
        "inactive": "без записей",
        "bonuses": "с бонусами > 0",
    }
    name = segment_names.get(segment, segment)

    await callback.message.answer(
        f"📢 Рассылка <b>{name}</b>\n\n"
        f"Напишите текст сообщения:",
        reply_markup=admin_back_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(BroadcastState.waiting_message)
    await callback.answer()


def admin_back_keyboard():
    """Клавиатура возврата в админку."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
        ]
    )


@router.message(BroadcastState.waiting_message, F.text)
async def broadcast_preview(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Показывает превью рассылки."""
    broadcast_text = message.text
    await state.update_data(broadcast_text=broadcast_text)

    data = await state.get_data()
    segment = data.get("broadcast_segment", "all")

    # Считаем клиентов по сегменту
    query = select(func.count(User.id))
    if segment == "active":
        query = query.where(
            User.id.in_(
                select(Booking.user_id).where(Booking.status.in_(("pending", "confirmed")))
            )
        )
    elif segment == "inactive":
        query = query.where(
            User.id.notin_(
                select(Booking.user_id).where(Booking.status.in_(("pending", "confirmed")))
            )
        )
    elif segment == "bonuses":
        query = query.where(User.bonus_balance > 0)

    result = await session.execute(query)
    count = result.scalar() or 0

    segment_names = {
        "all": "всем клиентам",
        "active": "с активной записью",
        "inactive": "без записей",
        "bonuses": "с бонусами > 0",
    }
    seg_name = segment_names.get(segment, segment)

    await message.answer(
        f"📢 <b>Превью рассылки</b> ({count} клиентов, {seg_name})\n\n"
        f"<i>Так будет выглядеть сообщение:</i>\n"
        f"{'─' * 30}\n\n"
        f"{html_escape(broadcast_text)}\n\n"
        f"{'─' * 30}",
        reply_markup=admin_broadcast_confirm_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(BroadcastState.waiting_confirm)


@router.callback_query(BroadcastState.waiting_confirm, F.data == "bc_confirm")
async def broadcast_send(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Отправляет рассылку асинхронно с задержкой."""
    data = await state.get_data()
    broadcast_text = data.get("broadcast_text", "")
    segment = data.get("broadcast_segment", "all")

    # Фильтруем пользователей по сегменту
    query = select(User)
    if segment == "active":
        query = query.where(
            User.id.in_(
                select(Booking.user_id).where(Booking.status.in_(("pending", "confirmed")))
            )
        )
    elif segment == "inactive":
        query = query.where(
            User.id.notin_(
                select(Booking.user_id).where(Booking.status.in_(("pending", "confirmed")))
            )
        )
    elif segment == "bonuses":
        query = query.where(User.bonus_balance > 0)

    result = await session.execute(query)
    users = result.scalars().all()

    # Запускаем рассылку в фоновой задаче
    global _broadcast_task
    _broadcast_task = asyncio.create_task(
        _send_broadcast(callback.bot, users, broadcast_text, callback.from_user.id)
    )

    log_admin_action(callback.from_user.id, 'broadcast', segment, f'{len(users)} users')

    await callback.message.answer(
        f"📢 <b>Рассылка запущена!</b>\n\n"
        f"Отправляется {len(users)} клиентам...\n"
        f"Результат придёт отдельным сообщением.\n\n"
        f"Для отмены нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить рассылку", callback_data="bc_abort")]
            ]
        ),
    )
    await state.clear()
    await callback.answer()


_broadcast_task: asyncio.Task | None = None


async def _send_broadcast(
    bot: Bot, users: list, text: str, admin_id: int,
) -> None:
    """Фоновая отправка рассылки с задержкой между сообщениями."""
    global _broadcast_task
    from aiogram.exceptions import TelegramRetryAfter

    sent = 0
    failed = 0
    cancelled = False

    for i, user in enumerate(users, 1):
        # Проверяем не отменена ли рассылка
        if _broadcast_task and _broadcast_task.cancelled():
            cancelled = True
            break

        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                parse_mode=None,  # plain text — безопасно, без HTML injection
            )
            sent += 1
        except TelegramRetryAfter as e:
            # Flood control — ждём и повторяем
            logger.warning("Flood control: ждём %s сек", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=text,
                    parse_mode=None,
                )
                sent += 1
            except Exception:
                failed += 1
        except Exception as e:
            logger.warning("Не удалось отправить рассылку %s: %s", user.telegram_id, e)
            failed += 1

        # Задержка чтобы не упираться в лимиты Telegram
        await asyncio.sleep(0.5)

        # Прогресс каждые 50 сообщений
        if i % 50 == 0:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"📢 Прогресс: {i}/{len(users)} отправлено...",
                )
            except Exception:
                pass

    # Отправляем отчёт админу
    status_text = "отменена" if cancelled else "завершена"
    try:
        await bot.send_message(
            chat_id=admin_id,
            text=(
                f"✅ <b>Рассылка {status_text}!</b>\n\n"
                f"📤 Отправлено: {sent}\n"
                f"❌ Не доставлено: {failed}"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Не удалось отправить отчёт о рассылке: %s", e)


@router.callback_query(BroadcastState.waiting_confirm, F.data == "bc_cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена рассылки (до запуска)."""
    await callback.message.answer("❌ Рассылка отменена.")
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "bc_abort")
async def broadcast_abort(callback: CallbackQuery) -> None:
    """Отмена запущенной рассылки."""
    global _broadcast_task
    if _broadcast_task and not _broadcast_task.done():
        _broadcast_task.cancel()
        await callback.message.answer("⏹ Рассылка отменяется... Текущий прогресс сохранён.")
    else:
        await callback.message.answer("Рассылка уже завершена или не запущена.")
    await callback.answer()


# =============================================================================
# УПРАВЛЕНИЕ FAQ (админка)
# =============================================================================

@router.callback_query(F.data == "admin_faq")
async def admin_faq_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    """Меню управления FAQ."""
    result = await session.execute(select(FAQ).order_by(FAQ.order))
    faqs = result.scalars().all()

    text = f"❓ <b>Управление FAQ</b>\n\n"
    if faqs:
        text += f"Всего вопросов: {len(faqs)}\n\n"
        for faq_item in faqs:
            status = "✅" if faq_item.is_active else "❌"
            text += f"{status} {faq_item.id}. {html_escape(faq_item.question[:40])}...\n"
    else:
        text += "<i>Пока нет вопросов.</i>\n"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить вопрос", callback_data="faq_add")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
        ]
    )

    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# =============================================================================
# УПРАВЛЕНИЕ УСЛУГАМИ
# =============================================================================

@router.callback_query(F.data == "admin_services")
async def admin_services(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает список услуг для управления."""
    result = await session.execute(
        select(Service).order_by(Service.category, Service.name)
    )
    services = result.scalars().all()

    text = f"⚙️ <b>Управление услугами</b>\n\n"
    current_cat = ""
    for s in services:
        if s.category != current_cat:
            current_cat = s.category
            text += f"\n📁 <b>{html_escape(current_cat)}</b>\n"
        status = "✅" if s.is_active else "❌"
        text += f"  {status} {html_escape(s.name)} — {format_price(s.price)}\n"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить услугу", callback_data="svc_add")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
        ]
    )

    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# =============================================================================
# УПРАВЛЕНИЕ БОНУСАМИ
# =============================================================================

@router.callback_query(F.data == "admin_bonuses")
async def admin_bonuses(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает информацию о бонусной системе."""
    total_result = await session.execute(select(func.sum(User.bonus_balance)))
    total = total_result.scalar() or 0

    top_result = await session.execute(
        select(User)
        .where(User.bonus_balance > 0)
        .order_by(User.bonus_balance.desc())
        .limit(10)
    )
    top_users = top_result.scalars().all()

    text = (
        f"🎁 <b>Управление бонусами</b>\n\n"
        f"💰 Всего бонусов у клиентов: <b>{total}</b>\n"
        f"📌 {Config.BONUS_PERCENT}% при подтверждении записи\n"
        f"📌 Доп. бонусы после визита — вручную\n\n"
    )
    if top_users:
        text += "🏆 <b>Топ клиентов:</b>\n"
        for u in top_users:
            text += f"  • {html_escape(u.name)}: {u.bonus_balance} бонусов\n"
    else:
        text += "<i>Пока нет бонусов у клиентов.</i>\n"

    await callback.message.answer(
        text,
        reply_markup=admin_bonuses_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# =============================================================================
# СВЯЗАТЬСЯ С КЛИЕНТОМ (кнопка из уведомлений)
# =============================================================================

async def _start_admin_contact_dialog(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    telegram_id: int,
    client_name: str,
    client_phone: str,
    booking_id: int | None = None,
) -> None:
    """Запускает FSM-диалог отправки сообщения клиенту."""
    await state.clear()
    await state.update_data(
        reply_telegram_id=telegram_id,
        reply_booking_id=booking_id,
    )

    booking_line = f"📋 Запись: <b>#{booking_id}</b>\n" if booking_id else ""
    await callback.message.answer(
        f"📞 <b>Связаться с клиентом</b>\n\n"
        f"{booking_line}"
        f"👤 {html_escape(client_name)}\n"
        f"📱 {format_phone(client_phone)}\n"
        f"🆔 Telegram ID: <code>{telegram_id}</code>\n\n"
        f"Напишите текст сообщения для клиента:",
        parse_mode="HTML",
    )
    await state.set_state(AdminReplyState.waiting_reply)


@router.callback_query(F.data.startswith("admin_msg_"))
async def admin_msg_client(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Связаться с клиентом по заявке (кнопка из уведомления о записи)."""
    await callback.answer()

    try:
        booking_id = int(callback.data.removeprefix("admin_msg_"))
    except ValueError:
        await callback.message.answer("⚠️ Некорректная кнопка. Откройте /admin заново.")
        return

    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await callback.message.answer("⚠️ Заявка не найдена.")
        return

    client = booking.user
    await _start_admin_contact_dialog(
        callback,
        state,
        telegram_id=client.telegram_id,
        client_name=client.name,
        client_phone=client.phone,
        booking_id=booking_id,
    )


@router.callback_query(F.data.startswith("admin_contact_"))
async def admin_contact_client(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Связаться с клиентом по Telegram ID (вопрос/фото консультации)."""
    await callback.answer()

    try:
        telegram_id = int(callback.data.removeprefix("admin_contact_"))
    except ValueError:
        await callback.message.answer("⚠️ Некорректная кнопка.")
        return

    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    client = result.scalar_one_or_none()
    if not client:
        await callback.message.answer(
            f"⚠️ Клиент {telegram_id} не найден в базе.\n"
            f"Ответьте вручную: <code>/reply {telegram_id} текст</code>",
            parse_mode="HTML",
        )
        return

    await _start_admin_contact_dialog(
        callback,
        state,
        telegram_id=client.telegram_id,
        client_name=client.name,
        client_phone=client.phone,
    )


@router.message(AdminReplyState.waiting_reply, F.text)
async def process_admin_msg_reply(msg: Message, state: FSMContext) -> None:
    """Отправляет сообщение клиенту из FSM-диалога."""
    data = await state.get_data()
    target_id = data.get("reply_telegram_id")
    if not target_id:
        await msg.answer("⚠️ Клиент не найден. Начните заново через /admin.")
        await state.clear()
        return

    reply_text = html_escape(msg.text.strip())
    booking_id = data.get("reply_booking_id")

    try:
        await msg.bot.send_message(
            chat_id=target_id,
            text=(
                f"💬 <b>Сообщение от Ашуры"
                f"{f' по записи #{booking_id}' if booking_id else ''}:</b>\n\n"
                f"{reply_text}"
            ),
            parse_mode="HTML",
        )
        await msg.answer(f"✅ Сообщение отправлено клиенту {target_id}.")
    except Exception as e:
        logger.error("Ошибка отправки сообщения клиенту: %s", e)
        await msg.answer(f"❌ Не удалось отправить: {e}")

    await state.clear()


# =============================================================================
# ДОБАВЛЕНИЕ FAQ
# =============================================================================

@router.callback_query(F.data == "faq_add")
async def admin_faq_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало добавления вопроса в FAQ."""
    await callback.message.answer(
        "➕ <b>Новый вопрос FAQ</b>\n\nВведите текст вопроса:",
        reply_markup=admin_back_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(AdminFaqAddState.waiting_question)
    await callback.answer()


@router.message(AdminFaqAddState.waiting_question, F.text)
async def admin_faq_add_question(msg: Message, state: FSMContext) -> None:
    """Получает вопрос FAQ."""
    await state.update_data(faq_question=msg.text.strip())
    await msg.answer("Теперь введите ответ на этот вопрос:")
    await state.set_state(AdminFaqAddState.waiting_answer)


@router.message(AdminFaqAddState.waiting_answer, F.text)
async def admin_faq_add_answer(
    msg: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Сохраняет новый FAQ."""
    data = await state.get_data()
    question = data.get("faq_question", "").strip()
    answer = msg.text.strip()
    if not question:
        await msg.answer("⚠️ Вопрос не найден. Начните заново.")
        await state.clear()
        return

    result = await session.execute(select(func.max(FAQ.order)))
    max_order = result.scalar() or 0

    faq = FAQ(question=question, answer=answer, order=max_order + 1, is_active=True)
    session.add(faq)
    await session.flush()

    await msg.answer(
        f"✅ Вопрос добавлен в FAQ (#{faq.id}).\n\n"
        f"<b>{question}</b>\n{answer}",
        parse_mode="HTML",
    )
    await state.clear()


# =============================================================================
# ДОБАВЛЕНИЕ УСЛУГИ
# =============================================================================

@router.callback_query(F.data == "svc_add")
async def admin_svc_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало добавления услуги."""
    await callback.message.answer(
        "➕ <b>Новая услуга</b>\n\nВведите название услуги:",
        reply_markup=admin_back_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(AdminServiceAddState.waiting_name)
    await callback.answer()


@router.message(AdminServiceAddState.waiting_name, F.text)
async def admin_svc_add_name(msg: Message, state: FSMContext) -> None:
    """Получает название услуги."""
    await state.update_data(svc_name=msg.text.strip())
    await msg.answer(
        "Введите категорию (например: «Уход за лицом», «Инъекции»):"
    )
    await state.set_state(AdminServiceAddState.waiting_category)


@router.message(AdminServiceAddState.waiting_category, F.text)
async def admin_svc_add_category(msg: Message, state: FSMContext) -> None:
    """Получает категорию услуги."""
    await state.update_data(svc_category=msg.text.strip())
    await msg.answer("Введите цену в рублях (число, например: 5000):")
    await state.set_state(AdminServiceAddState.waiting_price)


@router.message(AdminServiceAddState.waiting_price, F.text)
async def admin_svc_add_price(
    msg: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Сохраняет новую услугу."""
    try:
        price = int(msg.text.strip().replace(" ", "").replace("₽", ""))
    except ValueError:
        await msg.answer("⚠️ Введите число — цену в рублях:")
        return

    if price <= 0:
        await msg.answer("⚠️ Цена должна быть больше нуля. Попробуйте ещё раз:")
        return
    if price > 500_000:
        await msg.answer("⚠️ Слишком большая цена. Проверьте и попробуйте ещё раз:")
        return

    data = await state.get_data()
    name = data.get("svc_name", "").strip()
    category = data.get("svc_category", "").strip()
    if not name or not category:
        await msg.answer("⚠️ Данные потеряны. Начните заново.")
        await state.clear()
        return

    service = Service(name=name, category=category, price=price, is_active=True)
    session.add(service)
    await session.flush()

    await msg.answer(
        f"✅ Услуга добавлена!\n\n"
        f"💅 {name}\n"
        f"📁 {category}\n"
        f"💰 {format_price(price)}",
    )
    await state.clear()


# =============================================================================
# НАЧИСЛЕНИЕ БОНУСОВ
# =============================================================================

async def _bonus_client_choices(session: AsyncSession) -> list[tuple[int, str, int]]:
    """Список клиентов для выбора: сначала с бонусами, затем остальные."""
    result = await session.execute(
        select(User).order_by(User.bonus_balance.desc(), User.name).limit(15)
    )
    users = result.scalars().all()
    return [(u.telegram_id, u.name, u.bonus_balance) for u in users]


async def _find_user_for_bonus(
    session: AsyncSession, identifier: str,
) -> User | None:
    """Находит клиента по Telegram ID или номеру телефона."""
    raw = identifier.strip()
    phone = validate_phone(raw)
    if phone:
        result = await session.execute(select(User).where(User.phone == phone))
        return result.scalar_one_or_none()

    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return None

    try:
        tg_id = int(digits)
    except ValueError:
        return None

    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    return result.scalar_one_or_none()


async def _bonus_tx_already_revoked(
    session: AsyncSession, tx_id: int,
) -> bool:
    """Проверяет, отменяли ли уже это начисление."""
    result = await session.execute(
        select(BonusTransaction.id)
        .where(
            BonusTransaction.amount < 0,
            BonusTransaction.description.contains(f"Отмена начисления #{tx_id}"),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _fetch_bonus_transaction(
    session: AsyncSession, tx_id: int,
) -> BonusTransaction | None:
    """Транзакция с клиентом (без lazy-load)."""
    result = await session.execute(
        select(BonusTransaction)
        .options(joinedload(BonusTransaction.user))
        .where(BonusTransaction.id == tx_id)
    )
    return result.scalar_one_or_none()


async def _apply_bonus_revoke(
    session: AsyncSession,
    bot: Bot,
    user: User,
    amount: int,
    *,
    reason: str,
    admin_id: int = 0,
) -> tuple[bool, str]:
    """
    Списывает бонусы у клиента (отмена ошибочного начисления).
    Возвращает (уведомление_доставлено, текст_для_админа).
    """
    if amount <= 0:
        return False, "Сумма должна быть больше нуля."

    # Атомарное списание — защита от race condition
    from sqlalchemy import update as sa_update
    result = await session.execute(
        sa_update(User)
        .where(User.id == user.id, User.bonus_balance >= amount)
        .values(bonus_balance=User.bonus_balance - amount)
    )
    if result.rowcount == 0:
        return (
            False,
            f"Недостаточно бонусов на балансе ({user.bonus_balance} < {amount}). "
            f"Списывайте не больше текущего баланса.",
        )
    await session.refresh(user)

    await add_bonus_transaction(session, user.id, -amount, reason)
    await session.flush()

    if admin_id:
        log_admin_action(admin_id, 'revoke_bonus', f'user={user.telegram_id}', f'-{amount}')

    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"↩️ <b>Корректировка бонусов</b>\n\n"
                f"Списано: -{amount} бонусов\n"
                f"💰 Баланс: {user.bonus_balance} бонусов\n\n"
                f"Если есть вопросы — напишите Ашуре."
            ),
            parse_mode="HTML",
        )
        notified = True
    except Exception as e:
        logger.warning("Корректировка бонусов: уведомление не доставлено: %s", e)
        notified = False

    admin_line = (
        f"✅ Списано -{amount} → {html_escape(user.name)}\n"
        f"💰 Баланс: {user.bonus_balance}\n"
        f"{'Клиент уведомлён.' if notified else 'Уведомление не доставлено.'}"
    )
    return notified, admin_line


async def _prompt_bonus_revoke_list(
    message: Message, session: AsyncSession, user: User,
) -> None:
    """Показывает последние начисления клиента для отмены."""
    tx_result = await session.execute(
        select(BonusTransaction)
        .where(
            BonusTransaction.user_id == user.id,
            BonusTransaction.amount > 0,
        )
        .order_by(BonusTransaction.created_at.desc())
        .limit(10)
    )
    transactions = tx_result.scalars().all()

    if not transactions:
        await message.answer(
            f"⚠️ У {html_escape(user.name)} нет начислений для отмены.",
            reply_markup=admin_bonuses_menu_keyboard(),
        )
        return

    await message.answer(
        f"↩️ <b>Отмена начисления</b>\n\n"
        f"👤 {html_escape(user.name)}\n"
        f"💰 Баланс: <b>{user.bonus_balance}</b>\n\n"
        f"Выберите операцию для отмены\n"
        f"или введите сумму вручную: /bonus {user.telegram_id} -N",
        reply_markup=admin_bonus_revoke_tx_keyboard(
            transactions, user.telegram_id,
        ),
        parse_mode="HTML",
    )


async def _booking_bonus_already_granted(
    session: AsyncSession, booking_id: int,
) -> bool:
    """Проверяет, начислялись ли уже бонусы за завершённый визит."""
    result = await session.execute(
        select(BonusTransaction.id)
        .where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.amount > 0,
            BonusTransaction.description.contains("выполненную запись"),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _suggested_bonus_for_amount(amount: int | None) -> int:
    """Рекомендуемое начисление бонусов по сумме визита."""
    if not amount or amount < Config.BONUS_MIN_AMOUNT:
        return 0
    return amount * Config.BONUS_PERCENT // 100


async def _apply_bonus_grant(
    session: AsyncSession,
    bot: Bot,
    user: User,
    amount: int,
    *,
    reason: str = "Ручное начисление администратором",
    booking_id: int | None = None,
    visit_amount: int | None = None,
    admin_id: int = 0,
) -> bool:
    """
    Начисляет бонусы клиенту и пишет в историю.
    Возвращает False, если уведомление клиенту не доставлено.
    """
    # Атомарное начисление — защита от race condition
    from sqlalchemy import update as sa_update
    result = await session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=User.bonus_balance + amount)
    )
    if result.rowcount == 0:
        return False
    await session.refresh(user)
    await add_bonus_transaction(
        session, user.id, amount, reason, booking_id=booking_id,
    )
    await session.flush()

    if admin_id:
        booking_target = f'user={user.telegram_id}'
        log_admin_action(admin_id, 'grant_bonus', booking_target, f'+{amount}')

    client_lines = [
        "🎁 <b>Вам начислены бонусы!</b>\n",
        f"+{amount} бонусов",
    ]
    if visit_amount:
        client_lines.append(f"💰 Сумма визита: {format_price(visit_amount)}")
    client_lines.extend([
        f"💰 Ваш баланс: {user.bonus_balance} бонусов",
        "",
        "Используйте их при следующей записи! 💫",
    ])

    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text="\n".join(client_lines),
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        logger.warning(
            "Бонусы начислены %s, уведомление не доставлено: %s",
            user.telegram_id, e,
        )
        return False


async def _prompt_bonus_amount(
    message: Message, user: User, *, booking_id: int | None = None,
) -> None:
    """Показывает выбор суммы начисления для клиента."""
    booking_line = f"📋 Запись: <b>#{booking_id}</b>\n" if booking_id else ""
    await message.answer(
        f"💰 <b>Начисление бонусов</b>\n\n"
        f"{booking_line}"
        f"👤 {html_escape(user.name)}\n"
        f"📱 {format_phone(user.phone)}\n"
        f"💰 Текущий баланс: <b>{user.bonus_balance}</b> бонусов\n\n"
        f"Выберите сумму или введите свою:",
        reply_markup=admin_bonus_amount_keyboard(user.telegram_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "bonus_revoke")
async def admin_bonus_revoke_start(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Отмена ошибочного начисления бонусов."""
    await callback.answer()
    clients = [
        c for c in await _bonus_client_choices(session) if c[2] > 0
    ]
    if not clients:
        await callback.message.answer(
            "⚠️ Нет клиентов с бонусами для корректировки.\n"
            "Введите Telegram ID или телефон.",
            reply_markup=admin_bonuses_menu_keyboard(),
        )
        return

    await callback.message.answer(
        "↩️ <b>Отмена начисления</b>\n\n"
        "Выберите клиента:",
        reply_markup=admin_bonus_revoke_clients_keyboard(clients),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "bonus_revoke_manual")
async def admin_bonus_revoke_manual(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    """Ручной поиск клиента для отмены бонусов."""
    await state.clear()
    await callback.message.answer(
        "🔍 <b>Корректировка бонусов</b>\n\n"
        "Введите <b>Telegram ID</b> или <b>телефон</b> клиента:",
        reply_markup=admin_back_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(AdminBonusRevokeState.waiting_user)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^bonus_revoke_pick_(\d+)$"))
async def admin_bonus_revoke_pick(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Клиент выбран — список его начислений."""
    await callback.answer()
    tg_id = parse_callback_int(callback.data, "bonus_revoke_pick_")
    if tg_id is None:
        await callback.message.answer("⚠️ Ошибка данных.")
        return
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        await callback.message.answer("⚠️ Клиент не найден.")
        return

    await _prompt_bonus_revoke_list(callback.message, session, user)


@router.callback_query(F.data.regexp(r"^admin_revoke_bonus_bk_(\d+)$"))
async def admin_revoke_bonus_from_booking(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Отзывает бонус подтверждения для завершённой записи."""
    booking_id = parse_callback_int(callback.data, "admin_revoke_bonus_bk_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    # Find confirmation bonus transaction for this booking
    result = await session.execute(
        select(BonusTransaction)
        .options(joinedload(BonusTransaction.user))
        .where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.amount > 0,
            BonusTransaction.description.contains("при подтверждении"),
        )
    )
    tx = result.scalar_one_or_none()

    if not tx:
        await callback.answer("Бонус подтверждения не найден.", show_alert=True)
        return

    if await _bonus_tx_already_revoked(session, tx.id):
        await callback.answer("Этот бонус уже отозван.", show_alert=True)
        return

    user = tx.user
    _, admin_line = await _apply_bonus_revoke(
        session,
        callback.bot,
        user,
        tx.amount,
        reason=f"Отзыв бонуса записи #{booking_id}: {tx.description}",
        admin_id=callback.from_user.id,
    )
    await callback.answer("Бонус отозван")
    await callback.message.answer(admin_line, parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^bonus_revoke_tx_(\d+)$"))
async def admin_bonus_revoke_tx(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Отменяет выбранное начисление целиком."""
    tx_id = parse_callback_int(callback.data, "bonus_revoke_tx_")
    if tx_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    tx = await _fetch_bonus_transaction(session, tx_id)
    if not tx or tx.amount <= 0:
        await callback.answer("Операция не найдена!", show_alert=True)
        return

    if await _bonus_tx_already_revoked(session, tx_id):
        await callback.answer("Это начисление уже отменено.", show_alert=True)
        return

    user = tx.user
    _, admin_line = await _apply_bonus_revoke(
        session,
        callback.bot,
        user,
        tx.amount,
        reason=f"Отмена начисления #{tx.id}: {tx.description}",
        admin_id=callback.from_user.id,
    )
    await callback.answer("Начисление отменено")
    await callback.message.answer(admin_line, parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^bonus_revoke_custom_(\d+)$"))
async def admin_bonus_revoke_custom(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    """Ручной ввод суммы списания бонусов."""
    tg_id = parse_callback_int(callback.data, "bonus_revoke_custom_")
    if tg_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    await state.clear()
    await state.update_data(bonus_revoke_tg_id=tg_id)
    await callback.message.answer(
        "✏️ Введите, сколько бонусов <b>списать</b> у клиента\n"
        "(целое число больше 0):",
        parse_mode="HTML",
    )
    await state.set_state(AdminBonusRevokeState.waiting_amount)
    await callback.answer()


@router.message(AdminBonusRevokeState.waiting_user, F.text)
async def admin_bonus_revoke_user(
    msg: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Поиск клиента для корректировки бонусов."""
    user = await _find_user_for_bonus(session, msg.text)
    if not user:
        await msg.answer("⚠️ Клиент не найден.")
        return

    await state.clear()
    await _prompt_bonus_revoke_list(msg, session, user)


@router.message(AdminBonusRevokeState.waiting_amount, F.text)
async def admin_bonus_revoke_amount(
    msg: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Списывает указанное количество бонусов."""
    try:
        amount = int(msg.text.strip().replace(" ", ""))
    except ValueError:
        await msg.answer("⚠️ Введите целое число:")
        return

    data = await state.get_data()
    tg_id = data.get("bonus_revoke_tg_id")
    if not tg_id:
        await msg.answer("⚠️ Клиент не найден. Начните заново.")
        await state.clear()
        return

    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        await msg.answer("⚠️ Клиент не найден.")
        await state.clear()
        return

    _, admin_line = await _apply_bonus_revoke(
        session,
        msg.bot,
        user,
        amount,
        reason="Корректировка бонусов администратором",
        admin_id=msg.from_user.id,
    )
    await msg.answer(admin_line, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "bonus_grant")
async def admin_bonus_grant_start(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Выбор клиента для начисления бонусов."""
    await callback.answer()
    clients = await _bonus_client_choices(session)
    if not clients:
        await callback.message.answer(
            "⚠️ В базе пока нет клиентов.\n"
            "Введите Telegram ID после регистрации клиента в боте.",
            reply_markup=admin_bonuses_menu_keyboard(),
        )
        return

    await callback.message.answer(
        "💰 <b>Начисление бонусов</b>\n\n"
        "Выберите клиента из списка\n"
        "или введите ID / телефон вручную:",
        reply_markup=admin_bonus_clients_keyboard(clients),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "bonus_grant_manual")
async def admin_bonus_grant_manual(callback: CallbackQuery, state: FSMContext) -> None:
    """Ручной ввод Telegram ID или телефона клиента."""
    await state.clear()
    await state.update_data(bonus_booking_id=None)
    await callback.message.answer(
        "🔍 <b>Поиск клиента</b>\n\n"
        "Введите <b>Telegram ID</b> (из уведомления о записи)\n"
        "или <b>номер телефона</b> клиента:",
        reply_markup=admin_back_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(AdminBonusGrantState.waiting_user)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^bonus_pick_(\d+)$"))
async def admin_bonus_pick_client(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Клиент выбран из списка — показываем суммы."""
    await callback.answer()
    await state.update_data(bonus_booking_id=None)
    tg_id = parse_callback_int(callback.data, "bonus_pick_")
    if tg_id is None:
        await callback.message.answer("⚠️ Ошибка данных.")
        return
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        await callback.message.answer("⚠️ Клиент не найден.")
        return

    await _prompt_bonus_amount(callback.message, user)


@router.callback_query(F.data.regexp(r"^bonus_grant_bk_(\d+)$"))
async def admin_bonus_from_booking(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Начисление бонусов из карточки заявки."""
    await callback.answer()
    booking_id = parse_callback_int(callback.data, "bonus_grant_bk_")
    if booking_id is None:
        await callback.message.answer("⚠️ Ошибка данных.")
        return
    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await callback.message.answer("⚠️ Заявка не найдена.")
        return

    await state.update_data(bonus_booking_id=booking_id)
    await _prompt_bonus_amount(
        callback.message, booking.user, booking_id=booking_id,
    )


@router.callback_query(F.data.regexp(r"^bonus_auto_(\d+)$"))
async def admin_bonus_auto_from_visit(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Начисляет рекомендуемые бонусы после завершённого визита."""
    booking_id = parse_callback_int(callback.data, "bonus_auto_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await callback.answer("Заявка не найдена!", show_alert=True)
        return

    if await _booking_bonus_already_granted(session, booking_id):
        await callback.answer("Бонусы уже начислены за этот визит.", show_alert=True)
        return

    amount = _suggested_bonus_for_amount(booking.total_amount)
    if amount <= 0:
        await callback.answer(
            "Для этой суммы нет рекомендуемого начисления. Выберите другую сумму.",
            show_alert=True,
        )
        return

    user = booking.user
    notified = await _apply_bonus_grant(
        session,
        callback.bot,
        user,
        amount,
        reason=f"Начисление за выполненную запись #{booking_id}",
        booking_id=booking_id,
        visit_amount=booking.total_amount,
        admin_id=callback.from_user.id,
    )
    await callback.answer(f"+{amount} бонусов начислено!")

    notify_line = "Клиент уведомлён." if notified else "Уведомление не доставлено."
    await callback.message.answer(
        f"✅ <b>+{amount}</b> бонусов → {html_escape(user.name)}\n"
        f"📋 Запись #{booking_id}\n"
        f"💰 Баланс: <b>{user.bonus_balance}</b>\n"
        f"{notify_line}",
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^bonus_skip_(\d+)$"))
async def admin_bonus_skip_from_visit(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Пропуск начисления бонусов после визита."""
    booking_id = parse_callback_int(callback.data, "bonus_skip_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    booking = await _fetch_booking(session, booking_id)
    if not booking:
        await callback.answer("Заявка не найдена!", show_alert=True)
        return

    await callback.answer("Бонусы не начислены")
    await callback.message.answer(
        f"⏭ Бонусы за запись #{booking_id} ({html_escape(booking.user.name)}) не начислены."
    )


@router.callback_query(F.data.regexp(r"^bonus_add_(\d+)_(\d+)$"))
async def admin_bonus_add_preset(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Начисление выбранной суммы бонусов."""
    parts = callback.data.removeprefix("bonus_add_").split("_", 1)
    tg_id = int(parts[0])
    amount = int(parts[1])

    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Клиент не найден!", show_alert=True)
        return

    data = await state.get_data()
    booking_id = data.get("bonus_booking_id")
    visit_amount = None
    reason = "Ручное начисление администратором"
    if booking_id:
        if await _booking_bonus_already_granted(session, booking_id):
            await callback.answer(
                "Бонусы уже начислены за этот визит.", show_alert=True,
            )
            return
        booking = await _fetch_booking(session, booking_id)
        if booking and booking.user_id == user.id:
            visit_amount = booking.total_amount
            reason = f"Начисление за выполненную запись #{booking_id}"
        else:
            booking_id = None

    notified = await _apply_bonus_grant(
        session,
        callback.bot,
        user,
        amount,
        reason=reason,
        booking_id=booking_id,
        visit_amount=visit_amount,
        admin_id=callback.from_user.id,
    )
    await state.update_data(bonus_booking_id=None)
    await callback.answer(f"+{amount} бонусов начислено!")

    booking_line = f"📋 Запись #{booking_id}\n" if booking_id else ""
    notify_line = "Клиент уведомлён." if notified else "Уведомление не доставлено."
    await callback.message.answer(
        f"✅ <b>+{amount}</b> бонусов → {html_escape(user.name)}\n"
        f"{booking_line}"
        f"💰 Баланс: <b>{user.bonus_balance}</b>\n"
        f"{notify_line}",
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^bonus_add_custom_(\d+)$"))
async def admin_bonus_add_custom_start(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    """Ввод произвольной суммы бонусов."""
    tg_id = parse_callback_int(callback.data, "bonus_add_custom_")
    if tg_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    await state.clear()
    await state.update_data(bonus_target_tg_id=tg_id)
    await callback.message.answer(
        f"✏️ Введите количество бонусов для начисления\n"
        f"(целое число больше 0):",
    )
    await state.set_state(AdminBonusGrantState.waiting_amount)
    await callback.answer()


@router.message(AdminBonusGrantState.waiting_user, F.text)
async def admin_bonus_grant_user(
    msg: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Поиск клиента по ID или телефону."""
    user = await _find_user_for_bonus(session, msg.text)
    if not user:
        await msg.answer(
            "⚠️ Клиент не найден.\n"
            "Проверьте Telegram ID или телефон.\n"
            "Клиент должен быть зарегистрирован в боте (/start)."
        )
        return

    await state.clear()
    await _prompt_bonus_amount(msg, user)


@router.message(AdminBonusGrantState.waiting_amount, F.text)
async def admin_bonus_grant_amount(
    msg: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Начисляет произвольное количество бонусов."""
    admin_id = msg.from_user.id
    try:
        amount = int(msg.text.strip().replace(" ", ""))
    except ValueError:
        await msg.answer("⚠️ Введите целое число — количество бонусов:")
        return

    if amount <= 0:
        await msg.answer("⚠️ Количество должно быть больше нуля.")
        return
    if amount > 100_000:
        await msg.answer("⚠️ Слишком большая сумма. Максимум 100 000 бонусов за раз.")
        return

    data = await state.get_data()
    tg_id = data.get("bonus_target_tg_id")
    if not tg_id:
        await msg.answer("⚠️ Клиент не найден. Начните заново через /admin.")
        await state.clear()
        return

    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        await msg.answer(f"⚠️ Клиент с ID {tg_id} не найден в базе.")
        await state.clear()
        return

    data = await state.get_data()
    booking_id = data.get("bonus_booking_id")
    visit_amount = None
    reason = "Ручное начисление администратором"
    if booking_id:
        if await _booking_bonus_already_granted(session, booking_id):
            await msg.answer("⚠️ Бонусы уже начислены за этот визит.")
            await state.clear()
            return
        booking = await _fetch_booking(session, booking_id)
        if booking and booking.user_id == user.id:
            visit_amount = booking.total_amount
            reason = f"Начисление за выполненную запись #{booking_id}"
        else:
            booking_id = None

    notified = await _apply_bonus_grant(
        session,
        msg.bot,
        user,
        amount,
        reason=reason,
        booking_id=booking_id,
        visit_amount=visit_amount,
        admin_id=admin_id,
    )
    notify_line = "Клиент уведомлён." if notified else "Уведомление не доставлено."
    booking_line = f"📋 Запись #{booking_id}\n" if booking_id else ""
    await msg.answer(
        f"✅ Начислено +{amount} бонусов клиенту {html_escape(user.name)}.\n"
        f"{booking_line}"
        f"💰 Баланс: {user.bonus_balance}\n"
        f"{notify_line}"
    )
    await state.clear()


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, session: AsyncSession) -> None:
    """
    Начисление или списание: /bonus <id или телефон> <сумма>
    Отрицательная сумма — корректировка (отмена ошибки).
    """
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "⚠️ Использование:\n"
            "<code>/bonus &lt;telegram_id или телефон&gt; &lt;сумма&gt;</code>\n\n"
            "Начислить: <code>/bonus 526164603 500</code>\n"
            "Списать: <code>/bonus 526164603 -200</code>",
            parse_mode="HTML",
        )
        return

    user = await _find_user_for_bonus(session, parts[1])
    if not user:
        await message.answer("⚠️ Клиент не найден.")
        return

    try:
        amount = int(parts[2].strip().replace(" ", ""))
    except ValueError:
        await message.answer("⚠️ Сумма должна быть числом.")
        return

    if amount == 0:
        await message.answer("⚠️ Сумма не может быть нулём.")
        return

    if amount < 0:
        _, admin_line = await _apply_bonus_revoke(
            session,
            message.bot,
            user,
            abs(amount),
            reason="Корректировка бонусов (команда /bonus)",
            admin_id=message.from_user.id,
        )
        await message.answer(admin_line, parse_mode="HTML")
        return

    notified = await _apply_bonus_grant(session, message.bot, user, amount, admin_id=message.from_user.id)
    notify_line = "Клиент уведомлён." if notified else "Уведомление не доставлено."
    await message.answer(
        f"✅ +{amount} бонусов → {html_escape(user.name)}\n"
        f"💰 Баланс: {user.bonus_balance}\n"
        f"{notify_line}"
    )


# =============================================================================
# ОТВЕТ КЛИЕНТУ (/reply ID текст)
# =============================================================================

@router.message(Command("reply"))
async def cmd_reply(message: Message, session: AsyncSession) -> None:
    """
    Ответ клиенту: /reply 123456789 Ваш ответ здесь
    """
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "⚠️ Использование: <code>/reply &lt;telegram_id&gt; &lt;текст&gt;</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("⚠️ ID должен быть числом.")
        return

    reply_text = html_escape(parts[2])

    try:
        await message.bot.send_message(
            chat_id=target_id,
            text=(
                f"💬 <b>Ответ от Ашуры:</b>\n\n"
                f"{reply_text}\n\n"
                f"<i>Если остались вопросы — задайте их в разделе "
                f"'❓ FAQ / Консультация'!</i>"
            ),
            parse_mode="HTML",
        )
        await message.answer(f"✅ Ответ отправлен клиенту {target_id}.")
    except Exception as e:
        logger.error("Ошибка отправки ответа: %s", e)
        await message.answer(f"❌ Не удалось отправить: {e}")
