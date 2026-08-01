"""
Согласие на обработку персональных данных (152-ФЗ).
Показывается перед регистрацией; без согласия бот недоступен.
"""

import logging

from html import escape as html_escape
from typing import Any, Awaitable, Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from keyboards import (
    back_to_main_keyboard,
    privacy_after_read_keyboard,
    privacy_consent_keyboard,
    privacy_info_keyboard,
)
from utils.helpers import get_user_by_telegram_id
from utils.privacy import (
    CONSENT_DECLINED,
    CONSENT_INTRO,
    has_pd_consent,
    log_pd_consent,
    send_policy_text,
)
from utils.states import ConsentState, RegistrationState

logger = logging.getLogger(__name__)

router = Router()

_ADMIN_IDS = {aid for aid in (Config.ADMIN_ID, Config.owner_id()) if aid}


class PrivacyConsentMiddleware:
    """Блокирует бота без согласия на обработку ПДн (152-ФЗ)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update) or not event.event:
            return await handler(event, data)

        user_event = event.event
        tg_user = getattr(user_event, "from_user", None)
        if not tg_user or tg_user.is_bot:
            return  # Block bots, don't pass through

        if tg_user.id in _ADMIN_IDS:
            return await handler(event, data)

        state: FSMContext | None = data.get("state")
        if state:
            current = await state.get_state()
            if current and (
                current.startswith("ConsentState:")
                or current.startswith("RegistrationState:")
            ):
                return await handler(event, data)

        if isinstance(user_event, Message):
            text = (user_event.text or "").strip()
            if text.startswith(("/start", "/help", "/menu", "/restart")):
                return await handler(event, data)
        if isinstance(user_event, CallbackQuery) and (user_event.data or "").startswith(
            "privacy_"
        ):
            return await handler(event, data)

        session: AsyncSession | None = data.get("session")
        if not session:
            logger.warning("PrivacyConsentMiddleware: session is None, blocking request")
            return None

        db_user = await get_user_by_telegram_id(session, tg_user.id)
        if db_user and has_pd_consent(db_user):
            return await handler(event, data)

        msg = user_event if isinstance(user_event, Message) else user_event.message
        if not msg:
            return await handler(event, data)

        if db_user:
            await show_consent_for_existing_user(msg, state)
        else:
            await show_consent_for_registration(msg, state)

        if isinstance(user_event, CallbackQuery):
            await user_event.answer()
        return None


async def show_consent_for_registration(message: Message, state: FSMContext) -> None:
    """Согласие перед регистрацией нового клиента."""
    await state.clear()
    await state.set_state(ConsentState.waiting_accept)
    await state.update_data(consent_mode="registration")
    await message.answer(
        CONSENT_INTRO,
        reply_markup=privacy_consent_keyboard(for_registration=True),
        parse_mode="HTML",
    )


async def show_consent_for_existing_user(message: Message, state: FSMContext) -> None:
    """Согласие для уже зарегистрированного клиента без сохранённого согласия."""
    await state.clear()
    await state.set_state(ConsentState.waiting_accept)
    await state.update_data(consent_mode="existing")
    await message.answer(
        CONSENT_INTRO,
        reply_markup=privacy_consent_keyboard(for_registration=False),
        parse_mode="HTML",
    )


def _is_registration_mode(data: dict) -> bool:
    return data.get("consent_mode") == "registration"


@router.callback_query(ConsentState.waiting_accept, F.data == "privacy_read")
async def privacy_read(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает полный текст согласия."""
    data = await state.get_data()
    for_registration = _is_registration_mode(data)
    await send_policy_text(callback.message)
    await callback.message.answer(
        f"Версия документа: <b>{Config.PRIVACY_POLICY_VERSION}</b>",
        reply_markup=privacy_after_read_keyboard(for_registration=for_registration),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(ConsentState.waiting_accept, F.data.in_({"privacy_back_reg", "privacy_back_existing"}))
async def privacy_back_to_intro(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к краткому экрану согласия."""
    data = await state.get_data()
    for_registration = _is_registration_mode(data)
    await callback.message.answer(
        CONSENT_INTRO,
        reply_markup=privacy_consent_keyboard(for_registration=for_registration),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(ConsentState.waiting_accept, F.data == "privacy_accept_reg")
async def privacy_accept_registration(callback: CallbackQuery, state: FSMContext) -> None:
    """Согласие получено — переход к регистрации (имя)."""
    from utils.helpers import now_salon
    await state.update_data(pd_consent_at=now_salon().isoformat())
    await state.set_state(RegistrationState.waiting_name)
    await callback.message.answer(
        f"👋 <b>Добро пожаловать в {Config.SALON_NAME}!</b> 💫\n\n"
        f"Спасибо! Теперь пройдём короткую регистрацию.\n\n"
        f"Как вас зовут? (напишите ваше имя)",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(ConsentState.waiting_accept, F.data == "privacy_accept_existing")
async def privacy_accept_existing(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Согласие от существующего клиента — сохраняем в БД."""
    from handlers.common import show_client_home, _dispatch_deep_link, RegistrationState

    # Read deep link before state is cleared
    data = await state.get_data()
    deep_link = data.get('deep_link')

    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    await log_pd_consent(session, user)
    logger.info(
        "Согласие на ПДн: user_id=%s telegram_id=%s version=%s",
        user.id,
        user.telegram_id,
        Config.PRIVACY_POLICY_VERSION,
    )

    # Если имя/телефон были анонимизированы после revoke — просим заново
    if user.name.startswith("Удалён_") or user.phone == "00000000000":
        await state.set_state(RegistrationState.waiting_name)
        if deep_link:
            await state.update_data(deep_link=deep_link)
        await callback.message.answer(
            "✅ Согласие восстановлено!\n\n"
            "Давайте познакомимся заново. Как вас зовут? (напишите ваше имя)",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.clear()

    # Deep link: route to the requested feature instead of showing home
    if deep_link and await _dispatch_deep_link(
        callback.message, state, session, user, deep_link,
    ):
        await callback.answer()
        return

    await show_client_home(
        callback.message,
        f"✅ Спасибо! Согласие на обработку персональных данных сохранено.\n\n"
        f"👋 <b>{html_escape(user.name)}</b>, добро пожаловать в <b>{Config.SALON_NAME}</b>!\n\n"
        f"Выберите, что хотите сделать:",
    )
    await callback.answer()


@router.callback_query(ConsentState.waiting_accept, F.data == "privacy_decline")
async def privacy_decline(callback: CallbackQuery, state: FSMContext) -> None:
    """Отказ от согласия."""
    await state.clear()
    await callback.message.answer(CONSENT_DECLINED)
    await callback.answer()


@router.callback_query(F.data == "menu_privacy")
async def menu_privacy_info(callback: CallbackQuery, session: AsyncSession) -> None:
    """Раздел «Персональные данные» — ознакомление в любой момент."""
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    consent_line = ""
    if user and has_pd_consent(user):
        consent_at = user.pd_consent_at.strftime("%d.%m.%Y %H:%M")
        consent_line = (
            f"\n\n✅ Ваше согласие получено: <b>{consent_at}</b> "
            f"(версия {user.pd_consent_version or Config.PRIVACY_POLICY_VERSION})."
        )

    await callback.message.answer(
        f"📄 <b>Обработка персональных данных</b>\n\n"
        f"Оператор: <b>{Config.SALON_NAME}</b>\n"
        f"{Config.SALON_ADDRESS}\n\n"
        f"Вы можете в любой момент ознакомиться с текстом согласия "
        f"на обработку персональных данных (152-ФЗ)."
        f"{consent_line}",
        reply_markup=privacy_info_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "privacy_read_info")
async def privacy_read_info(callback: CallbackQuery) -> None:
    """Полный текст согласия из раздела меню."""
    await send_policy_text(callback.message)
    await callback.message.answer(
        f"Версия документа: <b>{Config.PRIVACY_POLICY_VERSION}</b>\n\n"
        f"Отозвать согласие можно кнопкой ниже.",
        reply_markup=privacy_info_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "privacy_revoke")
async def privacy_revoke(callback: CallbackQuery, session: AsyncSession) -> None:
    """Отзыв согласия на обработку ПДн."""
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user or not has_pd_consent(user):
        await callback.answer("Согласие уже отозвано.", show_alert=True)
        return

    # Audit trail ДО анонимизации (152-ФЗ: фиксируем факт и время отзыва)
    # НЕ логируем PII (имя, телефон) — это нарушает принцип минимизации данных
    try:
        from utils.audit import log_admin_action
        log_admin_action(
            user.telegram_id,
            "PDN_REVOKED",
            target=f"user:{user.telegram_id}",
            details="consent_revoked_pending_anonymization",
        )
    except Exception as e:
        logger.warning("PDN audit log failed: %s", e)

    user.pd_consent_at = None
    user.pd_consent_version = None
    # Очищаем ВСЕ анамнезы — нужно проходить заново
    user.anamnesis_json = None
    user.anamnesis_updated_at = None
    user.skin_anamnesis_json = None
    user.skin_anamnesis_at = None
    # Сохраняем ДО анонимизации (для notify админа)
    old_name = user.name
    old_phone = user.phone

    # Анонимизируем персональные данные (152-ФЗ)
    user.name = f"Удалён_{user.telegram_id}"
    user.phone = "00000000000"

    # Очищаем медицинские данные в записях (152-ФЗ: scrub bookings)
    from sqlalchemy import update as sa_update, select as sa_select, func as sa_func
    from database import Booking, User

    # Считаем активные записи ДО отмены (для уведомления админа)
    _active_count_result = await session.execute(
        sa_select(sa_func.count(Booking.id)).where(
            Booking.user_id == user.id,
            Booking.status.in_(('pending', 'confirmed')),
        )
    )
    _active_count = _active_count_result.scalar() or 0

    # Запоминаем ID активных записей с бонусами ДО отмены
    active_with_bonus_result = await session.execute(
        sa_select(Booking.id, Booking.bonus_used)
        .where(
            Booking.user_id == user.id,
            Booking.status.in_(('pending', 'confirmed')),
            Booking.bonus_used > 0
        )
    )
    active_bonus_bookings = active_with_bonus_result.all()  # list of (id, bonus_used)
    await session.flush()

    from utils.helpers import ACTIVE_BOOKING_STATUSES, STATUS_CANCELLED
    # Сначала отменяем активные записи, потом scrub
    await session.execute(
        sa_update(Booking)
        .where(Booking.user_id == user.id, Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        .values(status=STATUS_CANCELLED, anamnesis_json=None, notes=None)
    )
    # Scrub ВСЕ записи (включая завершённые/отменённые)
    await session.execute(
        sa_update(Booking)
        .where(Booking.user_id == user.id)
        .values(anamnesis_json=None, notes=None)
    )

    # Возвращаем бонусы ТОЛЬКО за только что отменённые записи
    total_refunded = 0
    for booking_id, bonus_amount in active_bonus_bookings:
        if bonus_amount > 0:
            await session.execute(
                sa_update(User).where(User.id == user.id)
                .values(bonus_balance=User.bonus_balance + bonus_amount)
            )
            total_refunded += bonus_amount
            await session.flush()
    for booking_id, _ in active_bonus_bookings:
        await session.execute(
            sa_update(Booking).where(Booking.id == booking_id)
            .values(bonus_used=0)
        )

    # Анонимизируем отзывы (152-ФЗ) — снимаем с публикации, чтобы пустой текст не отображался
    from database import Review
    await session.execute(
        sa_update(Review).where(Review.user_id == user.id)
        .values(text=None, is_published=False)
    )

    # Анонимизируем логи согласия (152-ФЗ: минимизация хранения)
    from database import PersonalDataConsentLog
    await session.execute(
        sa_update(PersonalDataConsentLog).where(PersonalDataConsentLog.user_id == user.id)
        .values(name=f'[анонимизировано_{user.telegram_id}]', phone='00000000000')
    )

    await session.flush()

    # Google Sheets cleanup — удаляем строку клиента
    try:
        from utils.google_sheets import delete_client_row
        await delete_client_row(user.telegram_id)
    except Exception as e:
        logger.debug("Sheets cleanup failed: %s", e)

    # Лог ПОСЛЕ успешной анонимизации (подтверждение завершения)
    try:
        from utils.audit import log_admin_action
        log_admin_action(
            user.telegram_id,
            "PDN_ANONYMIZED_OK",
            target=f"user:{user.telegram_id}",
            details=f"active_bookings_cancelled={_active_count} bonus_refunded={total_refunded}",
        )
    except Exception as e:
        logger.warning("PDN completion audit failed: %s", e)

    # Уведомляем админа об отзыве согласия
    try:
        from utils.helpers import send_message_to_owner, format_phone
        await send_message_to_owner(
            callback.bot,
            f'🚫 Клиент отозвал согласие на ПДн\n\n'
            f'👤 {old_name} (ID: {user.telegram_id})\n'
            f'📱 {format_phone(old_phone)}\n'
            f'📋 Отменено записей: {_active_count}\n\n'
            f'Данные анонимизированы.',
        )
    except Exception as e:
        logger.warning('PD revoke: не удалось уведомить админа: %s', e)

    await callback.message.answer(
        "🚫 <b>Согласие отозвано</b>\n\n"
        "Ваши персональные данные больше не обрабатываются.\n"
        "Для повторного использования бота необходимо дать согласие.\n\n"
        "Нажмите /start для повторного согласия.",
        parse_mode="HTML",
    )
    await callback.answer("Согласие отозвано.")