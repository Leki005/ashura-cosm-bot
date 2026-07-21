"""
Клиентские handlers:
- Главное меню
- Запись (анамнез → заявка)
- Услуги и цены
- Отзывы
- FAQ / Консультация
- Бонусная программа
- Контакты
- Мои записи
- Отмена записи
"""

import json
import logging
from datetime import datetime
from html import escape as html_escape
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    ANAMNESIS_QUESTIONS,
    admin_booking_keyboard,
    admin_consult_contact_keyboard,
    anamnesis_keyboard,
    anamnesis_summary_keyboard,
    back_to_main_keyboard,
    bonuses_menu_keyboard,
    active_booking_choice_keyboard,
    booking_date_keyboard,
    booking_procedure_keyboard,
    booking_skip_notes_keyboard,
    booking_time_keyboard,
    cancel_booking_keyboard,
    hide_quick_commands_keyboard,
    merge_confirm_keyboard,
    cancel_confirm_keyboard,
    consult_after_keyboard,
    contacts_keyboard,
    faq_detail_keyboard,
    faq_list_keyboard,
    faq_menu_keyboard,
    main_menu_keyboard,
    my_bookings_keyboard,
    post_procedure_feedback_keyboard,
    review_rating_keyboard,
    review_skip_text_keyboard,
    service_detail_keyboard,
    services_categories_keyboard,
    services_list_keyboard,
    use_bonuses_keyboard,
)
from utils.helpers import (
    ACTIVE_BOOKING_STATUSES,
    add_bonus_transaction,
    parse_callback_int,
    build_anamnesis_message,
    calculate_max_bonus_discount,
    cancel_booking_by_id,
    format_anamnesis,
    format_active_booking_prompt,
    format_booking_card,
    format_booking_services_line,
    format_contacts_text,
    format_date_from_callback,
    format_phone,
    format_time_from_callback,
    format_price,
    format_service_detail,
    get_active_booking,
    get_user_by_telegram_id,
    is_anamnesis_fresh,
    resolve_procedure_service,
    save_user_anamnesis,
    merge_service_into_booking,
    notify_admin_cancel,
    notify_admin_new_booking,
    notify_admin_procedure_feedback,
    notify_admin_service_merged,
    notify_client_booking_confirmed,
    send_message_to_owner,
    check_booking_date_allowed,
    check_booking_time_allowed,
    validate_booking_date,
    validate_booking_time,
)
from utils.states import (
    AnamnesisState,
    BookingState,
    ConsultationState,
    PostProcedureState,
    ReviewState,
)

logger = logging.getLogger(__name__)

router = Router()

# Double-submit guard: tracks users currently in _finalize_booking
_finalizing_users: set[int] = set()


# =============================================================================
# КЛАВИАТУРА ОТМЕНЫ FSM (новое — для всех FSM-состояний)
# =============================================================================

cancel_fsm_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_fsm")]
])


async def _hide_quick_commands(message: Message) -> None:
    """Убирает reply-клавиатуру /start на время пошаговой записи."""
    try:
        await message.answer("\u200b", reply_markup=hide_quick_commands_keyboard())
    except Exception:
        pass


def _build_anamnesis_json_from_answers(answers: dict) -> Optional[str]:
    """Собирает JSON анамнеза из ответов FSM."""
    if not answers:
        return None
    readable = {
        q_text: answers.get(q_key, False)
        for q_text, q_key in ANAMNESIS_QUESTIONS
    }
    return json.dumps(readable, ensure_ascii=False)


async def _ask_procedure_selection(message: Message, state: FSMContext) -> None:
    """Шаг выбора процедуры — после анамнеза, перед датой."""
    await message.answer(
        "💅 Выберите процедуру:\n"
        "или нажмите «Другое» и напишите название вручную.",
        reply_markup=booking_procedure_keyboard(),
    )
    await state.set_state(BookingState.waiting_procedure)


async def _ask_booking_date(message: Message, state: FSMContext) -> None:
    """Шаг выбора даты записи."""
    data = await state.get_data()
    proc = data.get("booking_service_name", "")
    proc_line = f"💅 Процедура: <b>{proc}</b>\n\n" if proc else ""
    await message.answer(
        f"{proc_line}"
        "📅 Выберите желаемую дату записи\n"
        "или напишите в чат (например: 15.07.2026):",
        reply_markup=booking_date_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(BookingState.waiting_date)


async def _proceed_after_anamnesis(
    message: Message, state: FSMContext, session: AsyncSession, user: User,
) -> None:
    """После анамнеза — всегда выбор процедуры (можно изменить предвыбранную)."""
    data = await state.get_data()
    preset = data.get("booking_service_name")
    if preset:
        await message.answer(
            f"💅 Сейчас выбрано: <b>{preset}</b>\n"
            f"Можете подтвердить или выбрать другую процедуру:",
            parse_mode="HTML",
        )
    await _ask_procedure_selection(message, state)


async def _start_general_booking(
    message: Message, state: FSMContext, session: AsyncSession, user: User,
) -> None:
    """Новая общая заявка — анамнез (если устарел) или сразу выбор процедуры."""
    import secrets
    await _hide_quick_commands(message)
    anam_token = secrets.token_hex(8)  # 16 hex chars — инвалидирует старые кнопки
    await state.update_data(
        anamnesis={},
        anam_index=0,
        anam_token=anam_token,
        booking_service_id=None,
        booking_service_name=None,
        merge_booking_id=None,
        use_stored_anamnesis=False,
    )

    if is_anamnesis_fresh(user):
        await state.update_data(use_stored_anamnesis=True)
        await message.answer(
            "📋 Ваша анкета актуальна (заполнена менее 7 дней назад).\n"
            "Повторно заполнять не нужно."
        )
        await _proceed_after_anamnesis(message, state, session, user)
        return

    await message.answer(
        build_anamnesis_message(0, {}),
        reply_markup=anamnesis_keyboard(0, {}, anam_token=anam_token),
    )
    await state.set_state(AnamnesisState.in_progress)


async def _start_service_booking(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    service: Service,
    user: User,
) -> None:
    """Новая запись на конкретную услугу (отдельное время)."""
    await state.update_data(
        booking_service_id=service.id,
        booking_service_name=service.name,
        anamnesis={},
        anam_index=0,
        merge_booking_id=None,
    )

    if user.bonus_balance > 0:
        max_discount = await calculate_max_bonus_discount(
            session, user.telegram_id, service.price,
        )
        if max_discount > 0:
            await _hide_quick_commands(message)
            await message.answer(
                f"🎁 У вас {user.bonus_balance} бонусов!\n"
                f"Скидка на {service.name} ({format_price(service.price)})\n\n"
                f"Максимум: {format_price(max_discount)}",
                reply_markup=use_bonuses_keyboard(service.price, user.bonus_balance),
            )
            await state.set_state(BookingState.waiting_bonus_amount)
            return

    if is_anamnesis_fresh(user):
        await state.update_data(use_stored_anamnesis=True)
        await _hide_quick_commands(message)
        await message.answer(
            f"📋 Анкета актуальна.\n"
            f"Запись на: <b>{service.name}</b>\n"
            f"Цена: {format_price(service.price)}",
            parse_mode="HTML",
        )
        await _proceed_after_anamnesis(message, state, session, user)
        return

    import secrets
    anam_token = secrets.token_hex(8)
    await state.update_data(anam_token=anam_token)
    await _hide_quick_commands(message)
    await message.answer(
        f"Запись на: {service.name}\n"
        f"Цена: {format_price(service.price)}\n\n"
        f"{build_anamnesis_message(0, {})}",
        reply_markup=anamnesis_keyboard(0, {}, anam_token=anam_token),
    )
    await state.set_state(AnamnesisState.in_progress)


# =============================================================================
# ГЛАВНОЕ МЕНЮ (callback'и)
# =============================================================================

@router.callback_query(F.data == "menu_booking")
async def menu_booking(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Начало записи — анамнез или выбор при активной записи."""
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    active_booking = await get_active_booking(session, user.id)
    if active_booking:
        await callback.message.answer(
            format_active_booking_prompt(active_booking),
            reply_markup=active_booking_choice_keyboard(active_booking.id, 0),
        )
        await callback.answer()
        return

    await _start_general_booking(callback.message, state, session, user)
    await callback.answer()


# =============================================================================
# АНАМНЕЗ: Ответы ДА/НЕТ
# =============================================================================

@router.callback_query(AnamnesisState.in_progress, F.data.startswith("anam_"))
async def anamnesis_answer(callback: CallbackQuery, state: FSMContext) -> None:
    """Обрабатывает ответы анамнеза (ДА/НЕТ)."""
    data = await state.get_data()
    answers = data.get("anamnesis", {})
    current_index = data.get("anam_index", 0)
    saved_token = data.get("anam_token", "")

    # Парсим callback_data: anam_{token}_{key}_{yes|no}
    # Токен — 16 hex chars, инвалидирует старые кнопки
    stripped = callback.data.removeprefix("anam_")

    # Проверяем токен сессии
    if saved_token:
        if not stripped.startswith(saved_token + "_"):
            # Старая кнопка — игнорируем
            await callback.answer("⏳ Эта кнопка устарела. Начните заново: /start", show_alert=True)
            return
        stripped = stripped.removeprefix(saved_token + "_")

    if "_yes" not in stripped and "_no" not in stripped:
        await callback.answer()
        return

    if stripped.endswith("_yes"):
        key = stripped.removesuffix("_yes")
        answer = True
    else:
        key = stripped.removesuffix("_no")
        answer = False

    if not key:
        await callback.answer()
        return

    # Сохраняем ответ
    answers[key] = answer
    current_index += 1

    await state.update_data(anamnesis=answers, anam_index=current_index)

    if current_index >= len(ANAMNESIS_QUESTIONS):
        # Все вопросы отвечены — показываем итог
        await state.set_state(AnamnesisState.completed)
        await show_anamnesis_summary(callback.message, answers)
    else:
        # Обновляем текст и кнопки в одном сообщении
        from aiogram.exceptions import TelegramBadRequest
        try:
            await callback.message.edit_text(
                build_anamnesis_message(current_index, answers),
                reply_markup=anamnesis_keyboard(current_index, answers, anam_token=saved_token),
            )
        except TelegramBadRequest:
            await callback.message.answer(
                build_anamnesis_message(current_index, answers),
                reply_markup=anamnesis_keyboard(current_index, answers, anam_token=saved_token),
            )

    await callback.answer()


async def show_anamnesis_summary(message: Message, answers: dict) -> None:
    """Показывает итоговую сводку анамнеза в одном сообщении."""
    from utils.text_format import split_message, wrap_lines

    lines = ["📋 Анкета заполнена!\n"]
    warnings = []

    for q_text, q_key in ANAMNESIS_QUESTIONS:
        answer = answers.get(q_key, False)
        icon = "❌" if answer else "✅"
        lines.append(wrap_lines(f"{icon} {q_text}", width=40))
        if answer:
            warnings.append(q_text)

    if warnings:
        lines.append("\n⚠️ Противопоказания:")
        lines.extend(wrap_lines(f"❌ {w}", width=40) for w in warnings)
        lines.append("\nАшура свяжется для уточнения.")
    else:
        lines.append("\n✅ Все показания в норме!")

    full_text = "\n".join(lines)
    chunks = split_message(full_text)
    for i, chunk in enumerate(chunks):
        markup = anamnesis_summary_keyboard() if i == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)


# =============================================================================
# ЗАПИСЬ: Продолжение после анамнеза
# =============================================================================

@router.callback_query(AnamnesisState.completed, F.data == "booking_continue")
async def booking_continue(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Продолжает запись после анамнеза — сохраняем в профиль на 7 дней."""
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    data = await state.get_data()
    anamnesis_json = _build_anamnesis_json_from_answers(data.get("anamnesis", {}))
    if anamnesis_json:
        await save_user_anamnesis(session, user, anamnesis_json)

    await callback.message.answer("✅ Анкета сохранена!")
    await _proceed_after_anamnesis(callback.message, state, session, user)
    await callback.answer()


# =============================================================================
# ЗАПИСЬ: Выбор процедуры
# =============================================================================

@router.callback_query(BookingState.waiting_procedure, F.data.startswith("proc_"))
async def booking_procedure_select(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Выбор процедуры из списка."""
    key = callback.data.replace("proc_", "")
    if key == "other":
        await callback.message.answer(
            "✏️ Напишите название процедуры:",
            reply_markup=cancel_fsm_kb,
        )
        await state.set_state(BookingState.waiting_procedure_custom)
        await callback.answer()
        return

    service_id, name = await resolve_procedure_service(session, key)
    await state.update_data(
        booking_service_id=service_id,
        booking_service_name=name,
    )
    await _ask_booking_date(callback.message, state)
    await callback.answer()


@router.message(BookingState.waiting_procedure_custom, F.text)
async def booking_procedure_custom(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Процедура «Другое» — ввод вручную."""
    if message.text in ("/start", "/restart", "/menu"):
        return

    service_id, name = await resolve_procedure_service(
        session, "other", message.text.strip(),
    )
    await state.update_data(
        booking_service_id=service_id,
        booking_service_name=name,
    )
    await _ask_booking_date(message, state)


# =============================================================================
# ЗАПИСЬ: Редактирование даты/времени (до подтверждения)
# =============================================================================

@router.callback_query(
    BookingState.waiting_time,
    F.data == "booking_edit_date",
)
@router.callback_query(
    BookingState.waiting_message,
    F.data == "booking_edit_date",
)
async def booking_edit_date(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору даты."""
    await _ask_booking_date(callback.message, state)
    await callback.answer()


@router.callback_query(
    BookingState.waiting_message,
    F.data == "booking_edit_time",
)
async def booking_edit_time(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к выбору времени."""
    data = await state.get_data()
    date_str = data.get("preferred_date")
    if not date_str:
        await _ask_booking_date(callback.message, state)
        await callback.answer("Сначала выберите дату.", show_alert=True)
        return
    await _ask_booking_time(callback.message, state, date_str)
    await callback.answer()


async def _ask_booking_time(message: Message, state: FSMContext, date_str: str) -> None:
    """Запрашивает время после выбора даты."""
    ok, err = check_booking_date_allowed(date_str)
    if not ok:
        await message.answer(f"⚠️ {err}", reply_markup=booking_date_keyboard())
        await state.set_state(BookingState.waiting_date)
        return

    await state.update_data(preferred_date=date_str)
    await message.answer(
        f"📅 Дата: <b>{date_str}</b>\n\n"
        "⏰ Выберите время или напишите (например: 14:00):",
        reply_markup=booking_time_keyboard(date_str),
        parse_mode="HTML",
    )
    await state.set_state(BookingState.waiting_time)


async def _ask_booking_notes(message: Message, state: FSMContext) -> None:
    """Запрашивает необязательные пожелания перед отправкой заявки."""
    data = await state.get_data()
    await message.answer(
        f"📅 {data.get('preferred_date', '—')}, ⏰ {data.get('preferred_time', '—')}\n\n"
        "📝 Дополнительные пожелания (необязательно).\n"
        "Напишите комментарий или нажмите «Пропустить».",
        reply_markup=booking_skip_notes_keyboard(),
    )
    await state.set_state(BookingState.waiting_message)


# =============================================================================
# ЗАПИСЬ: Выбор даты
# =============================================================================

@router.callback_query(BookingState.waiting_date, F.data.startswith("bdate_"))
async def booking_date_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Дата из кнопки или переход к ручному вводу."""
    code = callback.data.replace("bdate_", "")
    if code == "manual":
        await callback.message.answer(
            "✏️ Напишите дату в формате ДД.ММ.ГГГГ (например: 15.07.2026):",
            reply_markup=cancel_fsm_kb,
        )
        await callback.answer()
        return

    date_str = format_date_from_callback(code)
    await _ask_booking_time(callback.message, state, date_str)
    await callback.answer()


@router.message(BookingState.waiting_date, F.text)
async def booking_date_text(message: Message, state: FSMContext) -> None:
    """Дата, введённая текстом."""
    if message.text in ("/start", "/restart", "/menu"):
        return

    date_str = validate_booking_date(message.text)
    if not date_str:
        await message.answer(
            "⚠️ Дата недоступна.\n"
            "Нельзя записаться на прошедший день.\n"
            "Пример: 15.07.2026 или 15.07",
            reply_markup=booking_date_keyboard(),
        )
        return

    await _ask_booking_time(message, state, date_str)


# =============================================================================
# ЗАПИСЬ: Выбор времени
# =============================================================================

@router.callback_query(BookingState.waiting_time, F.data.startswith("btime_"))
async def booking_time_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Время из кнопки или переход к ручному вводу."""
    code = callback.data.replace("btime_", "")
    if code == "manual":
        await callback.message.answer(
            "✏️ Напишите время, например: 14:00 или 9:30:",
            reply_markup=cancel_fsm_kb,
        )
        await callback.answer()
        return

    time_str = format_time_from_callback(code)
    data = await state.get_data()
    date_str = data.get("preferred_date", "")
    ok, err = check_booking_time_allowed(date_str, time_str)
    if not ok:
        await callback.answer(err, show_alert=True)
        return

    await state.update_data(preferred_time=time_str)
    await _ask_booking_notes(callback.message, state)
    await callback.answer()


@router.message(BookingState.waiting_time, F.text)
async def booking_time_text(message: Message, state: FSMContext) -> None:
    """Время, введённое текстом."""
    if message.text in ("/start", "/restart", "/menu"):
        return

    time_str = validate_booking_time(message.text)
    data = await state.get_data()
    date_str = data.get("preferred_date", "")
    if not time_str:
        await message.answer(
            "⚠️ Не удалось распознать время.\n"
            "Пример: 14:00 или 9:30",
            reply_markup=booking_time_keyboard(date_str or None),
        )
        return

    ok, err = check_booking_time_allowed(date_str, time_str)
    if not ok:
        await message.answer(
            f"⚠️ {err}",
            reply_markup=booking_time_keyboard(date_str or None),
        )
        return

    await state.update_data(preferred_time=time_str)
    await _ask_booking_notes(message, state)


@router.callback_query(AnamnesisState.completed, F.data == "booking_restart_anam")
async def booking_restart_anam(callback: CallbackQuery, state: FSMContext) -> None:
    """Перезапуск анамнеза."""
    import secrets
    anam_token = secrets.token_hex(8)
    await state.update_data(anamnesis={}, anam_index=0, anam_token=anam_token)
    await callback.message.answer(
        build_anamnesis_message(0, {}),
        reply_markup=anamnesis_keyboard(0, {}, anam_token=anam_token),
    )
    await state.set_state(AnamnesisState.in_progress)
    await callback.answer()


# =============================================================================
# ЗАПИСЬ: Пожелания и сохранение в БД
# =============================================================================

async def _finalize_booking(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    notes: Optional[str],
    *,
    # При callback message.from_user — это бот; нужен ID нажавшего кнопку
    actor_telegram_id: Optional[int] = None,
    actor_username: Optional[str] = None,
) -> None:
    """Создаёт запись в БД с датой, временем и уведомляет админа."""
    tg_id = actor_telegram_id or message.from_user.id

    if tg_id in _finalizing_users:
        logger.warning('Double-submit blocked for user %s', tg_id)
        await message.answer('⏳ Обработка предыдущей заявки...', reply_markup=back_to_main_keyboard())
        return
    _finalizing_users.add(tg_id)

    try:
        tg_username = actor_username if actor_username is not None else message.from_user.username
        data = await state.get_data()

        await state.clear()

        preferred_date = data.get("preferred_date")
        preferred_time = data.get("preferred_time")

        if not preferred_date or not preferred_time:
            await message.answer(
                "⚠️ Не указаны дата или время. Начните запись заново.",
                reply_markup=back_to_main_keyboard(),
            )
            return

        ok, err = check_booking_time_allowed(preferred_date, preferred_time)
        if not ok:
            await message.answer(
                f"⚠️ {err}\n\nВыберите дату и время заново.",
                reply_markup=back_to_main_keyboard(),
            )
            return

        # user ищем по ID клиента (для callback «Пропустить» — actor_telegram_id)
        user = await get_user_by_telegram_id(session, tg_id)
        if not user:
            logger.warning(
                "Запись: пользователь tg_id=%s не найден в БД (from_user=%s)",
                tg_id, message.from_user.id,
            )
            await message.answer(
                "⚠️ Не удалось сохранить заявку. Нажмите /start и попробуйте снова.",
                reply_markup=back_to_main_keyboard(),
            )
            return

        # Recheck: защита от race condition (двойной submit)
        existing_active = await get_active_booking(session, user.id)
        if existing_active:
            await message.answer(
                "⚠️ У вас уже есть активная запись.\n"
                "Дождитесь её завершения или отмените через «Мои записи».",
                reply_markup=back_to_main_keyboard(),
            )
            return

        if data.get("use_stored_anamnesis"):
            anamnesis_json = user.anamnesis_json
        else:
            anamnesis_json = _build_anamnesis_json_from_answers(
                data.get("anamnesis", {}),
            )

        service_id = data.get("booking_service_id")
        bonus_used = data.get("bonus_used", 0)

        if bonus_used > 0:
            # Серверная валидация: баланс + лимит 50%
            if service_id:
                svc_check = await session.execute(select(Service).where(Service.id == service_id))
                svc_obj = svc_check.scalar_one_or_none()
                if svc_obj:
                    max_by_percent = svc_obj.price * Config.BONUS_MAX_DISCOUNT_PERCENT // 100
                    bonus_used = min(bonus_used, max_by_percent)
            # Атомарное списание бонусов (защита от race condition)
            from sqlalchemy import update
            result = await session.execute(
                update(User)
                .where(User.id == user.id, User.bonus_balance >= bonus_used)
                .values(bonus_balance=User.bonus_balance - bonus_used)
            )
            if result.rowcount == 0:
                await message.answer(
                    "⚠️ Недостаточно бонусов. Выберите меньшую сумму.",
                    reply_markup=back_to_main_keyboard(),
                )
                return
            await session.refresh(user)

        booking = Booking(
            user_id=user.id,
            service_id=service_id,
            preferred_date=preferred_date,
            preferred_time=preferred_time,
            status="pending",
            anamnesis_json=anamnesis_json,
            notes=notes,
            bonus_used=bonus_used,
        )
        session.add(booking)
        try:
            await session.flush()  # booking.id доступен после flush; commit сделает middleware
        except IntegrityError:
            await message.answer(
                "⚠️ У вас уже есть активная запись. Дождитесь её завершения.",
                reply_markup=back_to_main_keyboard(),
            )
            return

        if bonus_used > 0:
            await add_bonus_transaction(
                session,
                user.id,
                -bonus_used,
                f"Списание бонусов при записи #{booking.id}",
                booking_id=booking.id,
            )

        await session.refresh(booking)

        service = None
        if service_id:
            svc_result = await session.execute(select(Service).where(Service.id == service_id))
            service = svc_result.scalar_one_or_none()

        resolved_service_name = (
            service.name if service
            else data.get("booking_service_name", "Запись по заявке")
        )

        notify_ok = False
        try:
            await notify_admin_new_booking(
                message.bot,
                booking.id,
                user,
                service,
                anamnesis_json,
                notes,
                bonus_used=bonus_used,
                telegram_id=tg_id,
                service_name=resolved_service_name,
                username=tg_username,
                preferred_date=preferred_date,
                preferred_time=preferred_time,
            )
            notify_ok = True
        except Exception as e:
            logger.error("Ошибка уведомления админа: %s", e)

        from handlers.common import show_booking_success

        from html import escape as html_esc
        services_line = html_esc(resolved_service_name)
        notes_text = f"📝 Пожелания: {html_esc(notes)}\n" if notes else ""
        if notify_ok:
            notify_line = "Ашура получила уведомление и свяжется с вами."
        else:
            notify_line = (
                "⚠️ Не удалось связаться с Ашурой автоматически.\n"
                "Пожалуйста, напишите ей напрямую: "
                f"{format_phone(Config.SALON_PHONE)}"
            )
        await show_booking_success(
            message,
            "✅ Заявка отправлена!\n\n"
            f"💅 Процедуры: {services_line}\n"
            f"📅 Дата: {preferred_date}\n"
            f"⏰ Время: {preferred_time}\n"
            f"{notes_text}\n"
            f"{notify_line}\n\n"
            f"📱 {format_phone(user.phone)}\n\n"
            "💫 Ждём вас!",
        )
        await state.clear()
    finally:
        _finalizing_users.discard(tg_id)


@router.callback_query(BookingState.waiting_message, F.data == "booking_skip_notes")
async def booking_skip_notes(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Пропуск пожеланий — сразу создаём заявку."""
    # callback.from_user — клиент; callback.message.from_user — бот
    await _finalize_booking(
        callback.message,
        state,
        session,
        notes=None,
        actor_telegram_id=callback.from_user.id,
        actor_username=callback.from_user.username,
    )
    await callback.answer()


@router.message(BookingState.waiting_message, F.text)
async def booking_notes_message(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Получает необязательные пожелания и сохраняет заявку."""
    if message.text in ("/start", "/restart", "/menu"):
        return

    notes = message.text.strip()
    if notes in ("-", "—", "нет", "Нет"):
        notes = None
    elif notes and len(notes) > 500:
        await message.answer(
            "⚠️ Слишком длинный текст. Пожалуйста, сократите до 500 символов.",
            reply_markup=cancel_fsm_kb,
        )
        return

    await _finalize_booking(message, state, session, notes=notes)


# =============================================================================
# УСЛУГИ И ЦЕНЫ
# =============================================================================

@router.callback_query(F.data == "menu_services")
async def menu_services(callback: CallbackQuery) -> None:
    """Показывает категории услуг."""
    await callback.message.answer(
        f"💅 Услуги и цены\n\n"
        f"Выберите категорию:",
        reply_markup=services_categories_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cat_"))
async def show_services_by_category(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Показывает услуги в выбранной категории."""
    from keyboards import CATEGORY_MAP
    cat_key = callback.data.replace("cat_", "")
    category = CATEGORY_MAP.get(cat_key, cat_key)

    if category == "all":
        result = await session.execute(
            select(Service).where(Service.is_active == True).order_by(Service.category, Service.price)
        )
        services = result.scalars().all()
        header = "📋 Все услуги:\n"
    else:
        result = await session.execute(
            select(Service)
            .where(Service.category == category, Service.is_active == True)
            .order_by(Service.price)
        )
        services = result.scalars().all()
        header = f"📋 {category}:\n"

    if not services:
        await callback.message.answer(
            f"{header}\nВ этой категории пока нет услуг.",
            reply_markup=services_categories_keyboard(),
        )
        await callback.answer()
        return

    # Показываем список
    await callback.message.answer(
        header,
        reply_markup=services_list_keyboard(services, category),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^svc_\d+$"))
async def show_service_detail(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Показывает детальную карточку услуги."""
    service_id = parse_callback_int(callback.data, "svc_")
    if service_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    result = await session.execute(select(Service).where(Service.id == service_id))
    service = result.scalar_one_or_none()

    if not service:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    # Карточка услуги: текст без кнопок, кнопки отдельным сообщением внизу
    detail_parts = format_service_detail(service)
    for part in detail_parts:
        await callback.message.answer(part)
    # Кнопки отдельным сообщением — Telegram прокрутит вниз автоматически
    await callback.message.answer("👇", reply_markup=service_detail_keyboard(service.id))
    await callback.answer()


# =============================================================================
# ЗАПИСЬ НА КОНКРЕТНУЮ УСЛУГУ
# =============================================================================

@router.callback_query(F.data.startswith("book_svc_"))
async def book_service(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Начало записи на конкретную услугу."""
    service_id = parse_callback_int(callback.data, "book_svc_")
    if service_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    svc_result = await session.execute(
        select(Service).where(Service.id == service_id, Service.is_active == True)
    )
    service = svc_result.scalar_one_or_none()
    if not service:
        await callback.answer("Услуга недоступна.", show_alert=True)
        return

    active_booking = await get_active_booking(session, user.id)
    if active_booking:
        await callback.message.answer(
            format_active_booking_prompt(active_booking, service.name),
            reply_markup=active_booking_choice_keyboard(active_booking.id, service_id),
        )
        await callback.answer()
        return

    await _start_service_booking(callback.message, state, session, service, user)
    await callback.answer()


# =============================================================================
# ОБЪЕДИНЕНИЕ ЗАПИСЕЙ
# =============================================================================

@router.callback_query(F.data.regexp(r"^merge_combine_(\d+)_(\d+)$"))
async def merge_combine(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Показать текущую запись и запросить подтверждение объединения."""
    import re
    match = re.match(r"^merge_combine_(\d+)_(\d+)$", callback.data)
    if not match:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    booking_id = int(match.group(1))
    service_id = int(match.group(2))

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.service))
        .where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    booking = result.scalar_one_or_none()
    if not booking or booking.status not in ACTIVE_BOOKING_STATUSES:
        await callback.answer("Запись не найдена или уже закрыта.", show_alert=True)
        return

    service_name = None
    if service_id > 0:
        svc_result = await session.execute(
            select(Service).where(Service.id == service_id)
        )
        svc = svc_result.scalar_one_or_none()
        service_name = svc.name if svc else "Услуга"

    await callback.message.answer(
        format_active_booking_prompt(booking, service_name)
        + "\n\n🔗 Добавить к этой записи?",
        reply_markup=merge_confirm_keyboard(booking_id, service_id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^merge_confirm_(\d+)_(\d+)$"))
async def merge_confirm(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Подтверждённое объединение услуги с существующей записью."""
    from handlers.common import show_booking_success

    parts = callback.data.split("_")
    booking_id = int(parts[2])
    service_id = int(parts[3])

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.service))
        .where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    booking = result.scalar_one_or_none()
    if not booking or booking.status not in ACTIVE_BOOKING_STATUSES:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    if service_id > 0:
        svc_result = await session.execute(
            select(Service).where(Service.id == service_id)
        )
        service = svc_result.scalar_one_or_none()
        if not service:
            await callback.answer("Услуга не найдена.", show_alert=True)
            return

        merged = await merge_service_into_booking(session, booking, service)
        if not merged:
            await callback.answer(
                "Эта услуга уже есть в вашей записи.", show_alert=True,
            )
            return

        await session.refresh(booking)

        notify_ok = False
        try:
            await notify_admin_service_merged(
                callback.bot,
                booking,
                user,
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
                service=service,
            )
            notify_ok = True
        except Exception as e:
            logger.error("Ошибка уведомления об объединении: %s", e)

        notify_line = 'Ашура получила уведомление.' if notify_ok else '⚠️ Не удалось уведомить Ашуру. Напишите ей напрямую.'
        await show_booking_success(
            callback.message,
            "✅ Услуга добавлена к вашей записи!\n\n"
            f"#{booking.id} — {format_booking_services_line(booking)}\n"
            f"📅 {booking.preferred_date or '—'}, "
            f"⏰ {booking.preferred_time or '—'}\n\n"
            f"{notify_line}",
        )
        await state.clear()
        await callback.answer()
        return

    # Общая заявка без конкретной услуги — показываем выбор процедур
    await state.update_data(merge_booking_id=booking_id)
    await callback.message.answer(
        f"🔗 <b>Добавить к записи #{booking_id}</b>\n\n"
        f"Текущие услуги: {format_booking_services_line(booking)}\n"
        f"📅 {booking.preferred_date or '—'}, "
        f"⏰ {booking.preferred_time or '—'}\n\n"
        "💅 Выберите процедуру для добавления:",
        reply_markup=booking_procedure_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(BookingState.waiting_merge_procedure)
    await callback.answer()


@router.callback_query(BookingState.waiting_merge_procedure, F.data.startswith("proc_"))
async def merge_procedure_select(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Выбор процедуры для объединения с существующей записью."""
    from handlers.common import show_booking_success

    data = await state.get_data()
    booking_id = data.get("merge_booking_id")
    if not booking_id:
        await state.clear()
        await callback.answer("Ошибка. Попробуйте заново.", show_alert=True)
        return

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.service))
        .where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    booking = result.scalar_one_or_none()
    if not booking or booking.status not in ACTIVE_BOOKING_STATUSES:
        await callback.answer("Запись не найдена или уже закрыта.", show_alert=True)
        await state.clear()
        return

    key = callback.data.replace("proc_", "")
    if key == "other":
        await callback.message.answer(
            "✏️ Напишите название процедуры:",
            reply_markup=cancel_fsm_kb,
        )
        await state.set_state(BookingState.waiting_merge_custom)
        await callback.answer()
        return

    service_id, name = await resolve_procedure_service(session, key)
    svc_result = await session.execute(
        select(Service).where(Service.id == service_id)
    )
    service = svc_result.scalar_one_or_none()
    if not service:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    merged = await merge_service_into_booking(session, booking, service)
    if not merged:
        await callback.answer("Эта услуга уже есть в вашей записи.", show_alert=True)
        return

    await session.refresh(booking)

    notify_ok = False
    try:
        await notify_admin_service_merged(
            callback.bot, booking, user,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            service=service,
        )
        notify_ok = True
    except Exception as e:
        logger.error("Ошибка уведомления об объединении: %s", e)

    notify_line = 'Ашура получила уведомление.' if notify_ok else '⚠️ Не удалось уведомить Ашуру. Напишите ей напрямую.'
    await show_booking_success(
        callback.message,
        "✅ Услуга добавлена к вашей записи!\n\n"
        f"#{booking.id} — {format_booking_services_line(booking)}\n"
        f"📅 {booking.preferred_date or '—'}, "
        f"⏰ {booking.preferred_time or '—'}\n\n"
        f"{notify_line}",
    )
    await state.clear()
    await callback.answer()


@router.message(BookingState.waiting_merge_custom, F.text)
async def merge_procedure_custom(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Процедура «Другое» при объединении с записью."""
    from handlers.common import show_booking_success

    if message.text in ("/start", "/restart", "/menu"):
        return

    data = await state.get_data()
    booking_id = data.get("merge_booking_id")
    if not booking_id:
        await state.clear()
        return

    user = await get_user_by_telegram_id(session, message.from_user.id)
    if not user:
        return

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.service))
        .where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    booking = result.scalar_one_or_none()
    if not booking or booking.status not in ACTIVE_BOOKING_STATUSES:
        await message.answer("Запись не найдена.")
        await state.clear()
        return

    service_id, name = await resolve_procedure_service(
        session, "other", message.text.strip(),
    )
    svc_result = await session.execute(
        select(Service).where(Service.id == service_id)
    )
    service = svc_result.scalar_one_or_none()
    if not service:
        await message.answer("Услуга не найдена.")
        return

    merged = await merge_service_into_booking(session, booking, service)
    if not merged:
        await message.answer("Эта услуга уже есть в вашей записи.")
        return

    await session.refresh(booking)

    notify_ok = False
    try:
        await notify_admin_service_merged(
            message.bot, booking, user,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            service=service,
        )
        notify_ok = True
    except Exception as e:
        logger.error("Ошибка уведомления об объединении: %s", e)

    notify_line = 'Ашура получила уведомление.' if notify_ok else '⚠️ Не удалось уведомить Ашуру. Напишите ей напрямую.'
    await show_booking_success(
        message,
        "✅ Услуга добавлена к вашей записи!\n\n"
        f"#{booking.id} — {format_booking_services_line(booking)}\n"
        f"📅 {booking.preferred_date or '—'}, "
        f"⏰ {booking.preferred_time or '—'}\n\n"
        f"{notify_line}",
    )
    await state.clear()


@router.message(BookingState.waiting_merge_note, F.text)
async def merge_note_text(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Добавляет описание процедур к существующей записи."""
    from handlers.common import show_booking_success

    if message.text in ("/start", "/restart", "/menu"):
        return

    data = await state.get_data()
    booking_id = data.get("merge_booking_id")
    if not booking_id:
        await state.clear()
        return

    user = await get_user_by_telegram_id(session, message.from_user.id)
    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.service))
        .where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    booking = result.scalar_one_or_none()
    if not booking or booking.status not in ACTIVE_BOOKING_STATUSES:
        await message.answer("Запись не найдена.", reply_markup=back_to_main_keyboard())
        await state.clear()
        return

    addition = message.text.strip()
    note_line = f"+ Доп. процедуры: {addition}"
    booking.notes = f"{booking.notes}\n{note_line}" if booking.notes else note_line
    await session.flush()

    try:
        await notify_admin_service_merged(
            message.bot,
            booking,
            user,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            note_text=addition,
        )
    except Exception as e:
        logger.error("Ошибка уведомления об объединении: %s", e)

    await show_booking_success(
        message,
        "✅ Процедуры добавлены к вашей записи!\n\n"
        f"#{booking.id} — {format_booking_services_line(booking)}\n"
        f"📅 {booking.preferred_date or '—'}, "
        f"⏰ {booking.preferred_time or '—'}",
    )
    await state.clear()


@router.callback_query(F.data.regexp(r"^merge_separate_(\d+)$"))
async def merge_separate(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Запись на другое время невозможна — уже есть активная запись."""
    service_id = parse_callback_int(callback.data, "merge_separate_")
    if service_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    active_booking = await get_active_booking(session, user.id)
    booking_id = active_booking.id if active_booking else 0

    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Мои записи", callback_data="menu_my_bookings"),
    )
    builder.row(
        InlineKeyboardButton(
            text="🔗 Совместить",
            callback_data=f"merge_combine_{booking_id}_{service_id}",
        ),
    )
    builder.row(
        InlineKeyboardButton(text="🔙 В меню", callback_data="menu_main"),
    )

    from aiogram.exceptions import TelegramBadRequest
    try:
        await callback.message.edit_text(
            "⚠️ У вас уже есть активная запись.\n\n"
            "Отмените её в «Мои записи» или совместите услуги в один визит.",
            reply_markup=builder.as_markup(),
        )
    except TelegramBadRequest:
        await callback.message.answer(
            "⚠️ У вас уже есть активная запись.\n\n"
            "Отмените её в «Мои записи» или совместите услуги в один визит.",
            reply_markup=builder.as_markup(),
        )
    await callback.answer()


# =============================================================================
# БОНУСЫ: Выбор количества
# =============================================================================

@router.callback_query(BookingState.waiting_bonus_amount, F.data.startswith("bonus_use_"))
async def process_bonus_amount(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Обрабатывает выбор количества бонусов."""
    amount = parse_callback_int(callback.data, "bonus_use_")
    if amount is None or amount <= 0:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    # Серверная валидация: баланс + лимит 50%
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Ошибка пользователя.", show_alert=True)
        return
    data = await state.get_data()
    service_id = data.get("booking_service_id")
    svc_result = await session.execute(select(Service).where(Service.id == service_id))
    service = svc_result.scalar_one_or_none()
    if not service:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    max_by_percent = service.price * Config.BONUS_MAX_DISCOUNT_PERCENT // 100
    max_bonus = min(user.bonus_balance, max_by_percent)
    if amount > max_bonus:
        amount = max_bonus
    if amount < 0:
        amount = 0

    await state.update_data(bonus_used=amount)

    bonus_text = f"\n🎁 Скидка бонусами: -{format_price(amount)}" if amount > 0 else ""

    if user and is_anamnesis_fresh(user):
        await state.update_data(use_stored_anamnesis=True)
        await _hide_quick_commands(callback.message)
        await callback.message.answer(
            f"📋 Анкета актуальна.\n"
            f"Запись на: {service.name}\n"
            f"Цена: {format_price(service.price)}{bonus_text}",
        )
        await _proceed_after_anamnesis(callback.message, state, session, user)
        await callback.answer()
        return

    import secrets
    anam_token = secrets.token_hex(8)
    await state.update_data(anam_token=anam_token)
    await _hide_quick_commands(callback.message)
    await callback.message.answer(
        f"Запись на: {service.name}\n"
        f"Цена: {format_price(service.price)}{bonus_text}\n\n"
        f"{build_anamnesis_message(0, {})}",
        reply_markup=anamnesis_keyboard(0, {}, anam_token=anam_token),
    )
    await state.set_state(AnamnesisState.in_progress)
    await callback.answer()


# =============================================================================
# ОПРОС ПОСЛЕ ПРОЦЕДУРЫ (~1 час после визита)
# =============================================================================

async def _get_completed_booking(
    session: AsyncSession, booking_id: int, telegram_id: int,
) -> Booking | None:
    """Завершённая запись клиента для обратной связи."""
    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.user), joinedload(Booking.service))
        .join(User)
        .where(
            Booking.id == booking_id,
            Booking.status == "completed",
            User.telegram_id == telegram_id,
        )
    )
    return result.scalar_one_or_none()


@router.callback_query(F.data.regexp(r"^followup_(great|good)_(\d+)$"))
async def followup_quick_response(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Быстрый ответ «отлично» / «хорошо» на опрос после процедуры."""
    parts = callback.data.split("_")
    mood = parts[1]
    booking_id = int(parts[2])

    booking = await _get_completed_booking(
        session, booking_id, callback.from_user.id,
    )
    if not booking:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    feedback = "😊 Всё отлично!" if mood == "great" else "🙂 Хорошо"
    try:
        await notify_admin_procedure_feedback(
            callback.bot, booking, booking.user, feedback,
        )
    except Exception as e:
        logger.warning("Не удалось уведомить админа об отзыве: %s", e)

    # Убираем кнопки опроса чтобы нельзя было нажать повторно
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        f"Спасибо за ответ! 💛\n\n"
        f"Рады, что вам понравилось.\n"
        f"Если захотите — оставьте отзыв в разделе «★ Отзывы» "
        f"или запишитесь снова через главное меню.",
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^followup_text_(\d+)$"))
async def followup_text_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Клиент хочет рассказать подробнее."""
    booking_id = parse_callback_int(callback.data, "followup_text_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    booking = await _get_completed_booking(
        session, booking_id, callback.from_user.id,
    )
    if not booking:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    # Убираем кнопки опроса чтобы нельзя было нажать повторно
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.update_data(followup_booking_id=booking_id)
    await callback.message.answer(
        "💬 Расскажите, как прошла процедура:\n"
        "что понравилось, есть ли вопросы или пожелания.\n\n"
        "Ашура обязательно прочитает ваше сообщение 💛",
    )
    await state.set_state(PostProcedureState.waiting_feedback)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^followup_question_(\d+)$"))
async def followup_question(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Вопрос после процедуры — переводим в консультацию."""
    booking_id = parse_callback_int(callback.data, "followup_question_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    booking = await _get_completed_booking(
        session, booking_id, callback.from_user.id,
    )
    if not booking:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    # Убираем кнопки опроса
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    service_name = format_booking_services_line(booking)
    try:
        await notify_admin_procedure_feedback(
            callback.bot,
            booking,
            booking.user,
            f"❓ Есть вопрос после процедуры «{service_name}»",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить админа: %s", e)

    await callback.message.answer(
        "❓ Задайте ваш вопрос Ашуре:\n"
        "можно написать текст или отправить фото.",
        reply_markup=cancel_fsm_kb,
    )
    await state.set_state(ConsultationState.waiting_question)
    await callback.answer()


@router.message(PostProcedureState.waiting_feedback, F.text)
async def followup_feedback_text(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Получает развёрнутый отзыв после процедуры."""
    data = await state.get_data()
    booking_id = data.get("followup_booking_id")
    if not booking_id:
        await message.answer("⚠️ Сессия истекла. Напишите Ашуре через «FAQ / Консультация».")
        await state.clear()
        return

    booking = await _get_completed_booking(
        session, booking_id, message.from_user.id,
    )
    if not booking:
        await message.answer("⚠️ Запись не найдена.")
        await state.clear()
        return

    feedback = message.text.strip()
    try:
        from html import escape as html_esc
        await notify_admin_procedure_feedback(
            message.bot, booking, booking.user, html_esc(feedback),
        )
    except Exception as e:
        logger.error("Ошибка пересылки отзыва админу: %s", e)

    await message.answer(
        "✅ Спасибо за подробный отзыв!\n\n"
        "Ашура получила ваше сообщение и ответит при необходимости. 💛",
        reply_markup=back_to_main_keyboard(),
    )
    await state.clear()


# =============================================================================
# ОТЗЫВЫ
# =============================================================================

@router.callback_query(F.data == "menu_reviews")
async def menu_reviews(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает опубликованные отзывы и кнопку оставить свой."""
    result = await session.execute(
        select(Review)
        .options(joinedload(Review.user))
        .where(Review.is_published == True)
        .order_by(Review.created_at.desc())
        .limit(10)
    )
    reviews = result.scalars().unique().all()

    # Считаем средний рейтинг
    avg_result = await session.execute(
        select(Review.rating).where(Review.is_published == True)
    )
    ratings = avg_result.scalars().all()
    avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else 0

    text = f"★ Отзывы клиентов (средний: {avg_rating}/5)\n\n"

    if reviews:
        for r in reviews:
            stars = "★" * r.rating
            user_name = html_escape(r.user.name) if r.user else "Аноним"
            review_text = f"\n{html_escape(r.text)}" if r.text else ""
            text += f"{stars} {user_name}{review_text}\n\n"
    else:
        text += "Пока нет отзывов. Будьте первыми!\n\n"

    # Кнопка оставить отзыв
    builder = back_to_main_keyboard()
    builder.inline_keyboard.insert(
        0, [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="review_start")]
    )

    # Разбиваем длинные сообщения (>4096 символов)
    from utils.text_format import split_message as _split
    chunks = _split(text)
    for i, chunk in enumerate(chunks):
        markup = builder if i == len(chunks) - 1 else None
        await callback.message.answer(chunk, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "review_start")
async def review_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало оставления отзыва — выбор рейтинга."""
    await callback.message.answer(
        f"✍️ Оставьте отзыв\n\n"
        f"Как вы оцениваете работу Ашуры?",
        reply_markup=review_rating_keyboard(),
    )
    await state.set_state(ReviewState.waiting_rating)
    await callback.answer()


@router.callback_query(ReviewState.waiting_rating, F.data.startswith("rate_"))
async def review_rating(callback: CallbackQuery, state: FSMContext) -> None:
    """Получает рейтинг, просит текст."""
    rating = parse_callback_int(callback.data, "rate_")
    if rating is None:
        await callback.answer("Ошибка.", show_alert=True)
        return
    if rating not in range(1, 6):
        await callback.answer("Оценка от 1 до 5.", show_alert=True)
        return
    await state.update_data(rating=rating)

    await callback.message.answer(
        f"{'★' * rating} — Спасибо!\n\n"
        f"Напишите текст отзыва (необязательно):\n"
        f"Что понравилось? Результат, атмосфера?",
        reply_markup=review_skip_text_keyboard(),
    )
    await state.set_state(ReviewState.waiting_text)
    await callback.answer()


@router.callback_query(ReviewState.waiting_text, F.data == "review_skip_text")
async def review_skip_text(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Пропуск текстового отзыва — сохраняет или обновляет."""
    data = await state.get_data()
    edit_review_id = data.get("edit_review_id")

    if edit_review_id:
        result = await session.execute(select(Review).where(Review.id == edit_review_id))
        review = result.scalar_one_or_none()
        if review:
            review.rating = data["rating"]
            await session.flush()
            await callback.message.answer(
                "✅ Отзыв обновлён!",
                reply_markup=back_to_main_keyboard(),
            )
            await state.clear()
            await callback.answer()
            return

    review = await save_review(callback.bot, callback.from_user.id, state, session, text=None)
    await _show_review_saved(callback.message, review.id)
    await state.clear()
    await callback.answer()


@router.message(ReviewState.waiting_text, F.text)
async def review_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Получает текст отзыва — сохраняет или обновляет."""
    data = await state.get_data()
    edit_review_id = data.get("edit_review_id")

    if edit_review_id:
        result = await session.execute(select(Review).where(Review.id == edit_review_id))
        review = result.scalar_one_or_none()
        if review:
            review.rating = data["rating"]
            review.text = message.text
            await session.flush()
            await message.answer(
                "✅ Отзыв обновлён!",
                reply_markup=back_to_main_keyboard(),
            )
            await state.clear()
            return

    review = await save_review(message.bot, message.from_user.id, state, session, text=message.text)
    await _show_review_saved(message, review.id)
    await state.clear()


REVIEW_EDIT_MINUTES = 30


async def _show_review_saved(message: Message, review_id: int) -> None:
    """Показывает подтверждение сохранения отзыва с кнопкой редактирования."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="✏️ Редактировать отзыв",
        callback_data=f"review_edit_{review_id}",
    ))
    builder.row(InlineKeyboardButton(
        text="☰ Главное меню",
        callback_data="menu_main",
    ))
    await message.answer(
        "✅ Отзыв сохранён!\n\n"
        f"В течение {REVIEW_EDIT_MINUTES} минут вы можете отредактировать отзыв.\n"
        "После этого он будет отправлен Ашуре на проверку. 💫",
        reply_markup=builder.as_markup(),
    )


async def save_review(
    bot: Bot,
    telegram_id: int,
    state: FSMContext,
    session: AsyncSession,
    text: str = None,
) -> Review:
    """Сохраняет отзыв в БД. Возвращает объект Review."""
    data = await state.get_data()
    rating = data["rating"]

    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise ValueError("Пользователь не найден")

    review = Review(user_id=user.id, rating=rating, text=text)
    session.add(review)
    await session.flush()
    await session.refresh(review)

    return review


# =============================================================================
# РЕДАКТИРОВАНИЕ ОТЗЫВА (30 минут)
# =============================================================================

@router.callback_query(F.data.regexp(r"^review_edit_(\d+)$"))
async def review_edit_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Начало редактирования отзыва."""
    from datetime import timedelta
    from utils.helpers import now_salon

    review_id = parse_callback_int(callback.data, "review_edit_")
    if review_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    result = await session.execute(select(Review).where(Review.id == review_id))
    review = result.scalar_one_or_none()

    if not review:
        await callback.answer("Отзыв не найден.", show_alert=True)
        return

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user or review.user_id != user.id:
        await callback.answer("Это не ваш отзыв.", show_alert=True)
        return

    now = now_salon()
    if now - review.created_at > timedelta(minutes=REVIEW_EDIT_MINUTES):
        await callback.answer(
            f"Время редактирования истекло ({REVIEW_EDIT_MINUTES} мин).",
            show_alert=True,
        )
        return

    await state.update_data(edit_review_id=review_id, rating=review.rating)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="★", callback_data="edit_rate_1"),
        InlineKeyboardButton(text="★★", callback_data="edit_rate_2"),
        InlineKeyboardButton(text="★★★", callback_data="edit_rate_3"),
        InlineKeyboardButton(text="★★★★", callback_data="edit_rate_4"),
        InlineKeyboardButton(text="★★★★★", callback_data="edit_rate_5"),
    )
    await callback.message.answer(
        f"✏️ <b>Редактирование отзыва</b>\n\n"
        f"Текущая оценка: {'★' * review.rating}\n"
        f"Текст: {html_escape(review.text or '—')}\n\n"
        f"Выберите новую оценку:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.set_state(ReviewState.waiting_rating)
    await callback.answer()


@router.callback_query(ReviewState.waiting_rating, F.data.startswith("edit_rate_"))
async def review_edit_rating(callback: CallbackQuery, state: FSMContext) -> None:
    """Новый рейтинг при редактировании."""
    rating = parse_callback_int(callback.data, "edit_rate_")
    if rating is None or rating not in range(1, 6):
        await callback.answer("Ошибка.", show_alert=True)
        return
    await state.update_data(rating=rating)

    await callback.message.answer(
        f"{'★' * rating}\n\n"
        f"Напишите новый текст отзыва (или нажмите «Пропустить»):",
        reply_markup=review_skip_text_keyboard(),
    )
    await state.set_state(ReviewState.waiting_text)
    await callback.answer()


# =============================================================================
# FAQ / КОНСУЛЬТАЦИЯ
# =============================================================================

@router.callback_query(F.data == "menu_faq")
async def menu_faq(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает меню FAQ и консультации."""
    consult_result = await session.execute(
        select(Service).where(Service.name == "Консультация косметолога")
    )
    consult = consult_result.scalar_one_or_none()
    consult_price = format_price(consult.price) if consult else format_price(4000)

    await callback.message.answer(
        f"❓ FAQ / Консультация\n\n"
        f"Консультация косметолога — {consult_price}\n"
        f"Разбор кожи, план процедур, рекомендации.\n\n"
        f"Чем могу помочь?",
        reply_markup=faq_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "faq_list")
async def faq_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает список FAQ."""
    result = await session.execute(
        select(FAQ).where(FAQ.is_active == True).order_by(FAQ.order)
    )
    faqs = result.scalars().all()

    if not faqs:
        await callback.message.answer(
            f"📋 FAQ\n\nПока нет вопросов.",
            reply_markup=faq_menu_keyboard(),
        )
        await callback.answer()
        return

    await callback.message.answer(
        f"📋 Частые вопросы\n"
        f"Нажмите на вопрос, чтобы увидеть ответ:",
        reply_markup=faq_list_keyboard(list(faqs)),
    )
    await callback.answer()


@router.callback_query(
    F.data.regexp(r"^faq_\d+$"),
)
async def faq_detail(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает ответ на конкретный FAQ."""
    faq_id = parse_callback_int(callback.data, "faq_")
    if faq_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    result = await session.execute(select(FAQ).where(FAQ.id == faq_id))
    faq = result.scalar_one_or_none()

    if not faq:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return

    from utils.text_format import split_message, wrap_lines
    from html import escape as html_escape

    answer_text = wrap_lines(html_escape(faq.answer), width=42)
    header = f"❓ {wrap_lines(html_escape(faq.question), width=42)}\n\n"
    chunks = split_message(header + answer_text)
    for i, chunk in enumerate(chunks):
        markup = faq_detail_keyboard() if i == len(chunks) - 1 else None
        await callback.message.answer(chunk, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("faq_page_"))
async def faq_page(callback: CallbackQuery, session: AsyncSession) -> None:
    """Пагинация FAQ."""
    page = parse_callback_int(callback.data, "faq_page_")
    if page is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    result = await session.execute(
        select(FAQ).where(FAQ.is_active == True).order_by(FAQ.order)
    )
    faqs = result.scalars().all()

    from aiogram.exceptions import TelegramBadRequest
    try:
        await callback.message.edit_reply_markup(
            reply_markup=faq_list_keyboard(list(faqs), page)
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


# =============================================================================
# КОНСУЛЬТАЦИЯ: Задать вопрос
# =============================================================================

@router.callback_query(F.data == "consult_ask")
async def consult_ask(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало диалога 'Задать вопрос Ашуре'."""
    await callback.message.answer(
        f"💬 Задайте вопрос Ашуре\n\n"
        f"Напишите вопрос — Ашура ответит в ближайшее время.\n\n"
        f"Можно отправить текст, фото или голосовое.",
        reply_markup=cancel_fsm_kb,
    )
    await state.set_state(ConsultationState.waiting_question)
    await callback.answer()


@router.message(ConsultationState.waiting_question)
async def consult_question(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Получает вопрос и пересылает Ашуре."""
    # Находим пользователя
    result = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала пройдите регистрацию: /start")
        return

    forward_ok = False
    try:
        info_text = (
            f"💬 Вопрос от клиента\n\n"
            f"👤 {html_escape(user.name)}\n"
            f"📱 {format_phone(user.phone)}\n"
            f"🆔 ID: {user.telegram_id}"
        )
        await send_message_to_owner(
            message.bot,
            info_text,
            reply_markup=admin_consult_contact_keyboard(user.telegram_id),
        )
        await message.forward(chat_id=Config.owner_id())
        if Config.owner_id() != Config.ADMIN_ID:
            await message.forward(chat_id=Config.ADMIN_ID)
        forward_ok = True
    except Exception as e:
        logger.error("Ошибка пересылки вопроса: %s", e)

    forward_line = '✅ Вопрос отправлен Ашуре!' if forward_ok else '⚠️ Не удалось отправить вопрос. Напишите Ашуре напрямую.'
    await message.answer(
        f"{forward_line}\n\n"
        f"Она ответит вам в ближайшее время.\n\n"
        f"Ответ придёт прямо сюда, в этот чат.",
        reply_markup=consult_after_keyboard(),
    )
    await state.clear()


# =============================================================================
# КОНСУЛЬТАЦИЯ: Отправить фото
# =============================================================================

@router.callback_query(F.data == "consult_photo")
async def consult_photo(callback: CallbackQuery, state: FSMContext) -> None:
    """Просит отправить фото для консультации."""
    await callback.message.answer(
        f"📸 Отправьте фото для консультации\n\n"
        f"Ашура посмотрит и даст рекомендации.\n"
        f"Можно отправить несколько фото.\n\n"
        f"Фото будут переданы только Ашуре.",
        reply_markup=cancel_fsm_kb,
    )
    await state.set_state(ConsultationState.waiting_photo)
    await callback.answer()


@router.message(ConsultationState.waiting_photo, F.photo)
async def consult_photo_received(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Получает фото и пересылает Ашуре."""
    result = await session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала пройдите регистрацию: /start")
        return

    try:
        info_text = (
            f"📸 Фото от клиента для консультации\n\n"
            f"👤 {html_escape(user.name)}\n"
            f"📱 {format_phone(user.phone)}\n"
            f"🆔 ID: {user.telegram_id}"
        )
        await send_message_to_owner(
            message.bot,
            info_text,
            reply_markup=admin_consult_contact_keyboard(user.telegram_id),
        )
        await message.forward(chat_id=Config.owner_id())
        if Config.owner_id() != Config.ADMIN_ID:
            await message.forward(chat_id=Config.ADMIN_ID)
    except Exception as e:
        logger.error("Ошибка пересылки фото: %s", e)

    await message.answer(
        f"✅ Фото отправлено Ашуре!\n\n"
        f"Она изучит и даст рекомендации.\n"
        f"Если хотите отправить ещё — нажмите «📸 Фото для консультации» снова.",
        reply_markup=consult_after_keyboard(),
    )
    await state.clear()


# =============================================================================
# КОНТАКТЫ
# =============================================================================

@router.callback_query(F.data == "menu_contacts")
async def menu_contacts(callback: CallbackQuery) -> None:
    """Показывает контакты салона."""
    await callback.message.answer(
        format_contacts_text(),
        reply_markup=contacts_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu_contacts_faq")
async def menu_contacts_faq(callback: CallbackQuery) -> None:
    """Объединённый раздел «Контакты и FAQ»."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📞 Контакты", callback_data="menu_contacts"))
    builder.row(InlineKeyboardButton(text="📋 Частые вопросы", callback_data="faq_list"))
    builder.row(InlineKeyboardButton(text="💬 Задать вопрос Ашуре", callback_data="consult_ask"))
    builder.row(InlineKeyboardButton(text="📸 Фото для консультации", callback_data="consult_photo"))
    builder.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main"))
    await callback.message.answer(
        "❓ <b>Контакты и FAQ</b>\n\nВыберите раздел:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


# =============================================================================
# БОНУСНАЯ ПРОГРАММА
# =============================================================================

@router.callback_query(F.data == "menu_bonuses")
async def menu_bonuses(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает меню бонусной программы."""
    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    await callback.message.answer(
        f"🎁 Бонусная программа\n\n"
        f"💰 Ваш баланс: {user.bonus_balance} бонусов\n\n"
        f"Как это работает:\n"
        f"• При подтверждении записи — {Config.BONUS_PERCENT}% бонусов (скидка)\n"
        f"• Бонусами до {Config.BONUS_MAX_DISCOUNT_PERCENT}% скидки при записи\n\n"
        f"💫 Копите бонусы и экономьте!",
        reply_markup=bonuses_menu_keyboard(user.bonus_balance),
    )
    await callback.answer()


@router.callback_query(F.data == "bonus_balance")
async def bonus_balance_view(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает текущий баланс бонусов."""
    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return
    await callback.message.answer(
        f"💰 <b>Ваш баланс</b>\n\n"
        f"{user.bonus_balance} бонусов\n"
        f"(1 бонус = 1 ₽ скидки при записи)",
        reply_markup=bonuses_menu_keyboard(user.bonus_balance),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "bonus_history")
async def bonus_history_view(callback: CallbackQuery, session: AsyncSession) -> None:
    """История начислений и списаний бонусов."""
    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    tx_result = await session.execute(
        select(BonusTransaction)
        .where(BonusTransaction.user_id == user.id)
        .order_by(BonusTransaction.created_at.desc())
        .limit(15)
    )
    transactions = tx_result.scalars().all()

    if not transactions:
        text = "📜 <b>История бонусов</b>\n\nПока нет операций."
    else:
        lines = ["📜 <b>История бонусов</b>\n"]
        for tx in transactions:
            sign = "+" if tx.amount > 0 else ""
            date = tx.created_at.strftime("%d.%m.%Y")
            lines.append(f"{date}: {sign}{tx.amount} — {html_escape(tx.description)}")
        text = "\n".join(lines)

    await callback.message.answer(
        text,
        reply_markup=bonuses_menu_keyboard(user.bonus_balance),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "bonus_info")
async def bonus_info_view(callback: CallbackQuery, session: AsyncSession) -> None:
    """Подробно о бонусной программе."""
    result = await session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return
    await callback.message.answer(
        f"ℹ️ <b>Как работают бонусы</b>\n\n"
        f"1️⃣ Ашура подтверждает вашу запись → "
        f"начисляется <b>{Config.BONUS_PERCENT}%</b> от стоимости процедуры\n"
        f"2️⃣ При следующей записи можно списать бонусы "
        f"(до {Config.BONUS_MAX_DISCOUNT_PERCENT}% от цены)\n"
        f"3️⃣ Бонусы возвращаются, если запись отменена\n\n"
        f"💰 Ваш баланс: <b>{user.bonus_balance}</b> бонусов",
        reply_markup=bonuses_menu_keyboard(user.bonus_balance),
        parse_mode="HTML",
    )
    await callback.answer()


# =============================================================================
# МОИ ЗАПИСИ
# =============================================================================

@router.callback_query(F.data == "menu_my_bookings")
async def menu_my_bookings(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Показывает записи клиента."""
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    bookings_result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.service))
        .where(Booking.user_id == user.id)
        .order_by(Booking.created_at.desc())
        .limit(10)
    )
    bookings = bookings_result.scalars().unique().all()

    if not bookings:
        await callback.message.answer(
            f"📋 Мои записи\n\n"
            f"У вас пока нет записей.\n\n"
            f"Нажмите '🗓 Записаться к Ашуре' в меню!",
            reply_markup=my_bookings_keyboard(),
        )
        await callback.answer()
        return

    await callback.message.answer(
        "📋 Мои записи\n\n"
        "Ниже — ваши заявки. Активные можно отменить кнопкой «❌ Отменить запись».",
        reply_markup=my_bookings_keyboard(),
    )

    # Каждая запись отдельным сообщением — с кнопкой отмены для активных
    for b in bookings:
        card = format_booking_card(b)
        if b.status in ACTIVE_BOOKING_STATUSES:
            markup = cancel_booking_keyboard(b.id)
        else:
            markup = None
        await callback.message.answer(card, reply_markup=markup)

    await callback.answer()


# =============================================================================
# ОТМЕНА ЗАПИСИ (клиент)
# =============================================================================

@router.callback_query(F.data.startswith("cancel_book_"))
async def cancel_booking_start(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Начало отмены — показываем подтверждение (без ввода причины)."""
    booking_id = parse_callback_int(callback.data, "cancel_book_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.service))
        .where(Booking.id == booking_id)
    )
    booking = result.scalar_one_or_none()

    if not booking or booking.user_id != user.id:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    if booking.status not in ACTIVE_BOOKING_STATUSES:
        await callback.answer("Эту запись уже нельзя отменить.", show_alert=True)
        return

    service_name = booking.service.name if booking.service else "Запись по заявке"
    await callback.message.answer(
        f"❌ Отменить запись #{booking_id}?\n"
        f"💅 {service_name}\n\n"
        f"После отмены вы сможете записаться заново.",
        reply_markup=cancel_confirm_keyboard(booking_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_confirm_"))
async def cancel_booking_confirm(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    """Подтверждённая отмена — статус cancelled сохраняется в БД (commit в middleware)."""
    booking_id = parse_callback_int(callback.data, "cancel_confirm_")
    if booking_id is None:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    # Загружаем запись для проверки дедлайна (до отмены)
    from sqlalchemy import select as sa_select
    check_result = await session.execute(
        sa_select(Booking)
        .where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    booking_check = check_result.scalar_one_or_none()
    if not booking_check or booking_check.status not in ACTIVE_BOOKING_STATUSES:
        await callback.answer("Запись не найдена или уже отменена.", show_alert=True)
        return

    # Дедлайн: нельзя отменить за < N часов до приёма (настраивается в config)
    if booking_check.preferred_date and booking_check.preferred_time:
        from utils.helpers import booking_slot_datetime, now_salon
        from datetime import timedelta
        slot_dt = booking_slot_datetime(booking_check.preferred_date, booking_check.preferred_time)
        deadline = timedelta(hours=Config.CANCEL_DEADLINE_HOURS)
        if slot_dt and slot_dt - now_salon() < deadline:
            await callback.answer(
                f"❌ Отмена невозможна менее чем за {Config.CANCEL_DEADLINE_HOURS} ч. до приёма.\n"
                "Свяжитесь с Ашурой напрямую.",
                show_alert=True,
            )
            return

    booking = await cancel_booking_by_id(session, booking_id, user.id)
    if not booking:
        await callback.answer("Запись не найдена или уже отменена.", show_alert=True)
        return

    # Возвращаем бонусы если они были списаны — атомарно
    if booking.bonus_used and booking.bonus_used > 0:
        from sqlalchemy import update as sa_update
        await session.execute(
            sa_update(User)
            .where(User.id == user.id)
            .values(bonus_balance=User.bonus_balance + booking.bonus_used)
        )
        await session.refresh(user)
        await add_bonus_transaction(
            session,
            user.id,
            booking.bonus_used,
            f"Возврат бонусов при отмене записи #{booking_id}",
            booking_id=booking_id,
        )
        booking.bonus_used = 0

    # Отзываем бонус подтверждения (+3%), если был
    from sqlalchemy import select as sa_select
    from database import BonusTransaction
    conf_tx_result = await session.execute(
        sa_select(BonusTransaction).where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.amount > 0,
            BonusTransaction.description.contains("при подтверждении"),
        )
    )
    conf_tx = conf_tx_result.scalar_one_or_none()
    if conf_tx and conf_tx.amount > 0:
        # Атомарный отзыв — защита от race condition
        from sqlalchemy import update as sa_update, func as sa_func
        await session.execute(
            sa_update(User)
            .where(User.id == user.id)
            .values(bonus_balance=sa_func.max(0, User.bonus_balance - conf_tx.amount))
        )
        await session.refresh(user)
        await add_bonus_transaction(
            session,
            user.id,
            -conf_tx.amount,
            f"Отзыв бонуса при отмене записи #{booking_id}",
            booking_id=booking_id,
        )

    # Подгружаем услугу для уведомления админу
    if booking.service_id:
        svc_result = await session.execute(
            select(Service).where(Service.id == booking.service_id)
        )
        booking.service = svc_result.scalar_one_or_none()

    try:
        await notify_admin_cancel(
            callback.bot, user, booking, "Отменено клиентом через бота",
        )
    except Exception as e:
        logger.error("Ошибка уведомления об отмене: %s", e)

    await callback.message.answer(
        "✅ Запись отменена.\n\n"
        "Теперь вы можете записаться заново!",
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_abort")
async def cancel_booking_abort(callback: CallbackQuery) -> None:
    """Клиент передумал отменять запись."""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Запись не изменена.")


# =============================================================================
# ОТМЕНА FSM — хендлер (новое!)
# =============================================================================

@router.callback_query(F.data == "cancel_fsm")
async def cancel_fsm_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменяет любое FSM-состояние и возвращает в главное меню."""
    from handlers.common import show_client_home

    await state.clear()
    await show_client_home(callback.message, "✅ Отменено. Вы в главном меню:")
    await callback.answer()


# =============================================================================
# СВОБОДНЫЙ ТЕКСТ ВНЕ FSM — тихая подсказка
# =============================================================================

@router.message(F.text, ~F.text.startswith("/"))
async def ai_auto_help(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Если клиент пишет текст вне FSM — показываем меню без навязывания."""
    # Не мешаем если пользователь в FSM состоянии
    current_state = await state.get_state()
    if current_state:
        return

    user = await get_user_by_telegram_id(session, message.from_user.id)
    if not user:
        return

    await message.answer(
        "Используйте кнопки меню для навигации 👇",
        reply_markup=main_menu_keyboard(),
    )