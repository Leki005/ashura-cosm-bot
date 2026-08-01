"""
Общие handlers:
- /start — приветствие и проверка регистрации
- /restart — сброс текущего диалога
- Регистрация (имя + телефон)
- /help — помощь
- Возврат в главное меню
"""

import logging
from datetime import datetime
from html import escape as html_escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Contact, Message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from database import User
from keyboards import main_menu_keyboard, restart_confirm_keyboard
from utils.helpers import (
    get_active_booking,
    get_user_by_telegram_id,
    now_salon,
    validate_name,
    validate_phone,
)
from utils.privacy import has_pd_consent, log_pd_consent
from utils.states import RegistrationState, ReviewState

logger = logging.getLogger(__name__)

router = Router()


# =============================================================================
# Block group/supergroup messages — bot works only in private chats
# =============================================================================

@router.message(F.chat.type != "private")
async def _block_group_messages(message: Message) -> None:
    return


# =============================================================================
# UI: главное меню + быстрые команды внизу
# =============================================================================

async def show_client_home(
    message: Message,
    text: str,
    *,
    parse_mode: str = "HTML",
    restore_commands: bool = False,
) -> None:
    """Показывает inline-меню."""
    await message.answer(
        text,
        reply_markup=main_menu_keyboard(),
        parse_mode=parse_mode,
    )


async def show_booking_success(message: Message, text: str) -> None:
    """
    Финал записи: только inline-кнопка «В меню».
    Reply-клавиатуру не трогаем — вернётся при переходе в главное меню.
    """
    from keyboards import back_to_main_keyboard

    await message.answer(text, reply_markup=back_to_main_keyboard())


# =============================================================================
# DEEP LINK DISPATCHER
# =============================================================================

async def _dispatch_deep_link(
    message: Message, state: FSMContext, session: AsyncSession, user, payload: str,
) -> bool:
    """Routes deep link payload to the appropriate flow. Returns True if handled."""
    if payload == 'book':
        from handlers.client import _start_general_booking
        await _start_general_booking(message, state, session, user)
        return True

    if payload == 'review':
        from keyboards import review_rating_keyboard
        await message.answer(
            "✍️ Оставьте отзыв\n\n"
            "Как вы оцениваете работу Ашуры?",
            reply_markup=review_rating_keyboard(),
        )
        await state.set_state(ReviewState.waiting_rating)
        return True

    if payload.startswith('service_'):
        svc_id_str = payload.removeprefix('service_')
        if svc_id_str.isdigit():
            from sqlalchemy import select as sa_select
            from database import Service
            from handlers.client import _start_service_booking
            svc_result = await session.execute(
                sa_select(Service).where(Service.id == int(svc_id_str))
            )
            service = svc_result.scalar_one_or_none()
            if service:
                await _start_service_booking(message, state, session, service, user)
                return True

    return False


# =============================================================================
# /START
# =============================================================================

@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """
    Обработчик команды /start.
    Проверяет, зарегистрирован ли пользователь.
    Если нет — начинает регистрацию.
    Throttle уже проверен в ThrottlingMiddleware — дублировать не нужно.
    """
    await state.clear()

    # Deep link handling: extract payload from "/start <payload>"
    payload = ''
    if message.text and ' ' in message.text:
        payload = message.text.split(maxsplit=1)[1].strip()

    from handlers.privacy import (
        show_consent_for_existing_user,
        show_consent_for_registration,
    )

    # АДМИН: мгновенный доступ к админ-панели, без регистрации и согласия
    from handlers.admin import is_admin
    if await is_admin(message.from_user.id):
        # Гарантируем что админ есть в БД (для уведомлений и прочего)
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if not user:
            from database import User as UserModel
            user = UserModel(
                telegram_id=message.from_user.id,
                name=message.from_user.full_name or "Админ",
                phone="",
                pd_consent_at=now_salon(),
            )
            session.add(user)
            await session.flush()
        from keyboards import admin_main_keyboard
        await message.answer(
            f"🔐 <b>Админ-панель {Config.SALON_NAME}</b>\n\nВыберите раздел:",
            reply_markup=admin_main_keyboard(),
            parse_mode="HTML",
        )
        return

    user = await get_user_by_telegram_id(session, message.from_user.id)

    if user:
        if not has_pd_consent(user):
            await show_consent_for_existing_user(message, state)
            if payload:
                await state.update_data(deep_link=payload)
            return
        # Existing user with consent — try deep link first
        if payload and await _dispatch_deep_link(message, state, session, user, payload):
            return

        await show_client_home(
            message,
            f"👋 С возвращением, <b>{html_escape(user.name)}</b>!\n\n"
            f"Добро пожаловать в <b>{Config.SALON_NAME}</b>! 💫\n\n"
            f"Выберите, что хотите сделать:",
        )
    else:
        await show_consent_for_registration(message, state)
        if payload:
            await state.update_data(deep_link=payload)


# =============================================================================
# РЕГИСТРАЦИЯ: Имя
# =============================================================================

@router.message(RegistrationState.waiting_name, F.text)
async def process_name(message: Message, state: FSMContext) -> None:
    """Получает имя пользователя."""
    # Не перехватываем быстрые команды во время регистрации
    if message.text in ("/start", "/restart", "/menu"):
        return

    name = message.text.strip()

    if not validate_name(name):
        await message.answer(
            "⚠️ Имя должно быть от 1 до 50 символов. Попробуйте ещё раз:"
        )
        return

    await state.update_data(name=name)

    await message.answer(
        f"Приятно познакомиться, <b>{html_escape(name)}</b>! 😊\n\n"
        f"Теперь отправьте ваш номер телефона:\n"
        f"• Нажмите кнопку '📞 Отправить контакт' ниже, или\n"
        f"• Напишите номер вручную (например: 89885919401)",
        reply_markup=phone_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(RegistrationState.waiting_phone)


def phone_keyboard():
    """Клавиатура с кнопкой отправки контакта."""
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📞 Отправить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# =============================================================================
# РЕГИСТРАЦИЯ: Телефон (контакт)
# =============================================================================

@router.message(RegistrationState.waiting_phone, F.contact)
async def process_phone_contact(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Получает телефон через кнопку 'Отправить контакт'."""
    contact: Contact = message.contact

    # Проверка: чужой контакт (user_id может отсутствовать у старых клиентов)
    if contact.user_id is not None and contact.user_id != message.from_user.id:
        await message.answer(
            "⚠️ Пожалуйста, отправьте свой контакт, а не чужой.\n"
            "Нажмите кнопку «📞 Отправить контакт» ещё раз."
        )
        return

    phone = validate_phone(contact.phone_number)

    if not phone:
        await message.answer(
            "⚠️ Не удалось распознать номер. Попробуйте написать вручную:"
        )
        return

    await finish_registration(message, state, session, phone)


# =============================================================================
# РЕГИСТРАЦИЯ: Телефон (текст)
# =============================================================================

@router.message(RegistrationState.waiting_phone, F.text)
async def process_phone_text(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """Получает телефон текстом."""
    if message.text in ("/start", "/restart", "/menu"):
        return

    phone = validate_phone(message.text)

    if not phone:
        await message.answer(
            "⚠️ Номер должен быть в формате 79XXXXXXXXX или 89XXXXXXXXX.\n"
            "Попробуйте ещё раз:"
        )
        return

    await finish_registration(message, state, session, phone)


# =============================================================================
# ЗАВЕРШЕНИЕ РЕГИСТРАЦИИ
# =============================================================================

async def finish_registration(
    message: Message, state: FSMContext, session: AsyncSession, phone: str,
) -> None:
    """Завершает регистрацию и создаёт пользователя в БД."""
    from datetime import datetime, timezone

    data = await state.get_data()
    name = data["name"]
    consent_raw = data.get("pd_consent_at")
    from utils.helpers import now_salon
    consent_at = (
        datetime.fromisoformat(consent_raw) if consent_raw else now_salon()
    )

    # Проверяем — может пользователь уже есть (после revoke ПДн)
    existing = await get_user_by_telegram_id(session, message.from_user.id)
    if existing:
        existing.name = name
        existing.phone = phone
        existing.pd_consent_at = consent_at
        existing.pd_consent_version = Config.PRIVACY_POLICY_VERSION
        user = existing
    else:
        user = User(
            telegram_id=message.from_user.id,
            name=name,
            phone=phone,
            pd_consent_at=consent_at,
            pd_consent_version=Config.PRIVACY_POLICY_VERSION,
        )
        session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent /start — пользователь уже создан параллельным запросом
        await session.rollback()
        existing = await get_user_by_telegram_id(session, message.from_user.id)
        if existing:
            existing.name = name
            existing.phone = phone
            existing.pd_consent_at = consent_at
            existing.pd_consent_version = Config.PRIVACY_POLICY_VERSION
            user = existing
            await session.flush()
        else:
            logger.error("finish_registration: IntegrityError but user not found tg_id=%s", message.from_user.id)
            await message.answer("⚠️ Ошибка регистрации. Попробуйте /start снова.")
            return
    await log_pd_consent(session, user, consented_at=consent_at)

    logger.info(
        "Новый пользователь зарегистрирован: tg_id=%s, согласие ПДн v%s",
        message.from_user.id,
        Config.PRIVACY_POLICY_VERSION,
    )

    # Deep link: route to the requested feature instead of showing home
    deep_link = data.get('deep_link')
    deep_link_handled = False
    if deep_link:
        deep_link_handled = await _dispatch_deep_link(
            message, state, session, user, deep_link,
        )

    if not deep_link_handled:
        await show_client_home(
            message,
            f"✅ <b>Регистрация завершена!</b>\n\n"
            f"Добро пожаловать, <b>{html_escape(name)}</b>! 💫\n\n"
            f"Теперь вы можете записаться на приём, узнать о наших услугах, "
            f"получить консультацию и многое другое!",
        )

    # Уведомляем админа о новом клиенте
    try:
        await message.bot.send_message(
            chat_id=Config.ADMIN_ID,
            text=(
                f"👤 <b>Новый клиент зарегистрировался!</b>\n\n"
                f"Имя: {html_escape(name)}\n"
                f"Телефон: +7{phone[1:]}\n"
                f"🆔 Telegram ID: {message.from_user.id}\n"
                f"Telegram: @{message.from_user.username or '—'}"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить админа о регистрации: %s", e)

    if not deep_link_handled:
        await state.clear()


# =============================================================================
# /HELP
# =============================================================================

@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """Показывает справку по боту."""
    await state.clear()

    await show_client_home(
        message,
        f"❓ <b>Помощь по боту {Config.SALON_NAME}</b>\n\n"
        f"<b>🗓 Записаться к Ашуре</b> — Оставьте заявку, Ашура свяжется для согласования времени\n\n"
        f"<b>📋 Мои записи</b> — Ваши текущие и прошлые записи\n\n"
        f"<b>💅 Услуги и цены</b> — Полный каталог с описанием\n\n"
        f"<b>✨ Подбор ухода с ИИ</b> — Анкета по коже + анализ фото\n\n"
        f"<b>🤖 Помощник ИИ</b> — Задайте вопрос ИИ-консультанту\n\n"
        f"<b>🎁 Бонусы</b> — Скидка за лояльность: 0% → 3% → 5%\n\n"
        f"<b>❓ Контакты и FAQ</b> — Адрес, телефон, частые вопросы\n\n"
        f"<b>★ Отзывы</b> — Оставьте отзыв или почитайте другие\n\n"
        f"<b>/menu</b> — вернуться в главное меню в любой момент\n\n"
        f"Если остались вопросы — нажмите «Контакты и FAQ» в меню!",
    )


# =============================================================================
# ВОЗВРАТ В ГЛАВНОЕ МЕНЮ
# =============================================================================

@router.callback_query(F.data == "menu_main")
async def back_to_main(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    """Возврат в главное меню."""
    from handlers.privacy import show_consent_for_existing_user

    await state.clear()
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if user and not has_pd_consent(user):
        await show_consent_for_existing_user(callback.message, state)
        await callback.answer()
        return
    await show_client_home(
        callback.message,
        f"👋 Вы в главном меню <b>{Config.SALON_NAME}</b>!\n\n"
        f"Выберите действие:",
    )
    await callback.answer()


@router.message(Command("menu"))
async def cmd_menu(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """В любой момент возвращает клиента в главное меню."""
    from handlers.privacy import show_consent_for_existing_user

    await state.clear()
    user = await get_user_by_telegram_id(session, message.from_user.id)
    if user:
        if not has_pd_consent(user):
            await show_consent_for_existing_user(message, state)
            return
        await show_client_home(
            message,
            f"👋 <b>{html_escape(user.name)}</b>, вы в главном меню!\n\n"
            f"Выберите, что хотите сделать:",
        )
    else:
        await message.answer(
            f"👋 Для начала работы нажмите /start и пройдите регистрацию.",
            parse_mode="HTML",
        )


# =============================================================================
# /RESTART — сброс диалога (Reply-кнопка и команда)
# =============================================================================


async def _do_restart(message: Message, state: FSMContext) -> None:
    """Сбрасывает FSM, согласие и анамнез. Записи в БД не трогаем."""
    await state.clear()
    # Сбрасываем согласие и анамнез в БД
    from database import async_session, User
    from sqlalchemy import update as sa_update
    async with async_session() as session:
        await session.execute(
            sa_update(User)
            .where(User.telegram_id == message.from_user.id)
            .values(pd_consent_at=None, pd_consent_version=None,
                    skin_anamnesis_json=None, skin_anamnesis_at=None)
        )
        await session.commit()
    await show_client_home(
        message,
        f"🔄 Бот перезапущен.\n\n"
        f"Данные сброшены. Нажмите /start для начала.",
    )


@router.message(Command("restart"))
async def cmd_restart(
    message: Message, state: FSMContext, session: AsyncSession,
) -> None:
    """
    /restart — сброс текущего диалога.
    Доступен в любой момент через кнопку внизу экрана.
    """
    user = await get_user_by_telegram_id(session, message.from_user.id)
    if user and await get_active_booking(session, user.id):
        await message.answer(
            "⚠️ У вас есть активная запись на процедуру.\n\n"
            "Рестарт сбросит текущий диалог, но <b>запись останется</b>.\n"
            "Продолжить?",
            reply_markup=restart_confirm_keyboard(),
            parse_mode="HTML",
        )
        return

    await _do_restart(message, state)


@router.callback_query(F.data == "restart_confirm")
async def restart_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждённый рестарт — только сброс FSM."""
    await _do_restart(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "restart_cancel")
async def restart_cancel(callback: CallbackQuery) -> None:
    """Отмена рестарта — остаёмся в текущем состоянии."""
    await callback.answer("Остаёмся. Запись не изменена.", show_alert=True)


@router.callback_query(F.data == "crm_no_remind")
async def crm_no_remind(callback: CallbackQuery, session: AsyncSession) -> None:
    """Клиент нажал 'Не напоминать' в CRM-напоминании."""
    from utils.crm import handle_no_remind
    await handle_no_remind(callback, session)