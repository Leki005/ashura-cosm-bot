"""
ИИ-консультант Ашуры (Grok API).
Режим активируется из главного меню; все текстовые сообщения идут в Grok.
Поддерживает анализ фото кожи через Grok Vision API.
"""

import logging
import time
from collections import defaultdict
from html import escape as html_escape

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PhotoSize
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from handlers.common import show_client_home
from keyboards import ai_consultant_keyboard, back_to_main_keyboard
from utils.grok import GrokAPIError, ask_grok, ask_grok_vision
from utils.helpers import get_user_by_telegram_id
from utils.states import AIConsultantState
from utils.text_format import split_message

logger = logging.getLogger(__name__)

router = Router()

# --- Константы для анализа фото ---
PHOTO_MIN_WIDTH = 400       # Минимальная ширина в пикселях
PHOTO_MIN_HEIGHT = 400      # Минимальная высота в пикселях
PHOTO_MIN_FILE_SIZE = 30_000  # Минимальный размер файла (30KB)
PHOTO_RATE_LIMIT_SECONDS = 30  # Минимум секунд между фото
PHOTO_MAX_PER_SESSION = 10     # Максимум фото за сессию

# Защита от спама фото: {user_id: [timestamps]}
_photo_rate_limit: dict[int, list[float]] = defaultdict(list)
_photo_session_count: dict[int, int] = defaultdict(int)
_photo_cleanup_last: float = 0
_PHOTO_CLEANUP_INTERVAL = 3600  # Очистка каждый час

AI_WELCOME = (
    "💫 <b>ИИ-консультант Ашуры</b>\n\n"
    "Привет! Я виртуальный помощник — могу подсказать по уходу за кожей "
    "и рассказать о процедурах в общих чертах.\n\n"
    "📸 <b>Новинка!</b> Пришлите фото лица или зоны декольте — "
    "я оценю состояние кожи и дам рекомендации.\n\n"
    "⚠️ Фото должно быть <b>без макияжа</b> при <b>дневном освещении</b>.\n\n"
    "Напишите ваш вопрос текстом или пришлите фото.\n"
    "Для выхода — /menu или кнопка «Завершить общение»."
)

AI_UNAVAILABLE = (
    "😔 ИИ-консультант временно недоступен.\n\n"
    "Попробуйте позже или воспользуйтесь разделом "
    "«❓ FAQ / Консультация» — Ашура ответит лично."
)

PHOTO_TOO_SMALL = (
    "📏 Фото слишком маленькое или размытое.\n\n"
    "Для анализа нужно чёткое фото крупным планом.\n"
    "Сделайте снимок при дневном свете, без вспышки."
)

PHOTO_RATE_LIMITED = (
    "⏳ Подождите немного перед отправкой следующего фото.\n"
    "Это нужно для защиты от спама."
)

PHOTO_SESSION_LIMIT = (
    "📸 Вы отправили слишком много фото за эту сессию.\n"
    "Для продолжения — завершите общение и начните заново."
)


def _check_photo_rate(user_id: int) -> bool:
    """Проверяет rate limit для фото. Возвращает True если можно отправить."""
    global _photo_cleanup_last
    now = time.time()

    # Периодическая очистка неактивных пользователей
    if now - _photo_cleanup_last > _PHOTO_CLEANUP_INTERVAL:
        stale = [uid for uid, ts in _photo_rate_limit.items()
                 if not ts or now - ts[-1] > _PHOTO_CLEANUP_INTERVAL]
        for uid in stale:
            del _photo_rate_limit[uid]
        # Очищаем _photo_session_count по timestamp из _photo_rate_limit
        stale_count = [uid for uid in _photo_session_count
                       if uid not in _photo_rate_limit]
        for uid in stale_count:
            del _photo_session_count[uid]
        _photo_cleanup_last = now

    timestamps = _photo_rate_limit[user_id]
    # Очищаем старые записи
    _photo_rate_limit[user_id] = [
        ts for ts in timestamps if now - ts < PHOTO_RATE_LIMIT_SECONDS
    ]
    return len(_photo_rate_limit[user_id]) < 1


def _record_photo(user_id: int) -> None:
    """Записывает timestamp отправки фото."""
    _photo_rate_limit[user_id].append(time.time())
    _photo_session_count[user_id] += 1


def _check_photo_session_limit(user_id: int) -> bool:
    """Проверяет лимит фото за сессию. Возвращает True если можно."""
    return _photo_session_count[user_id] < PHOTO_MAX_PER_SESSION


def reset_photo_session(user_id: int) -> None:
    """Сбрасывает счётчик фото при выходе из AI-режима."""
    _photo_session_count.pop(user_id, None)
    _photo_rate_limit.pop(user_id, None)


async def _enter_ai_mode(message: Message, state: FSMContext) -> None:
    """Переводит пользователя в режим ИИ-консультанта."""
    # НЕ сбрасываем счётчик фото — AI_DAILY_LIMIT_PER_USER ограничивает
    await state.clear()
    await state.set_state(AIConsultantState.chatting)
    await state.update_data(ai_history=[])
    await message.answer(AI_WELCOME, reply_markup=ai_consultant_keyboard())


async def _exit_ai_mode(message: Message, state: FSMContext, user_id: int | None = None) -> None:
    """Выход из AI-режима в главное меню."""
    data = await state.get_data()
    # Сбрасываем счётчик фото
    from_user = message.from_user
    if from_user:
        reset_photo_session(user_id if user_id else from_user.id)
    await state.clear()
    await show_client_home(
        message,
        f"👋 Вы вышли из чата с ИИ-консультантом.\n\n"
        f"Вы в главном меню <b>{Config.SALON_NAME}</b>!",
    )


@router.callback_query(F.data == "menu_ai_consultant")
async def start_ai_consultant(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Вход в режим ИИ-консультанта из главного меню."""
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    if not Config.XAI_API_KEY:
        await callback.message.answer(
            AI_UNAVAILABLE,
            reply_markup=back_to_main_keyboard(),
        )
        await callback.answer()
        return

    await _enter_ai_mode(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "ai_exit")
async def exit_ai_consultant(callback: CallbackQuery, state: FSMContext) -> None:
    """Выход по кнопке «Завершить общение»."""
    try:
        await _exit_ai_mode(callback.message, state, user_id=callback.from_user.id)
    except Exception as e:
        logger.error("Ошибка выхода из AI: %s", e)
        await show_client_home(callback.message, "👋 Вы в главном меню!")
    await callback.answer()


@router.message(AIConsultantState.chatting, F.text)
async def ai_consultant_message(message: Message, state: FSMContext) -> None:
    """Обрабатывает текстовые сообщения в AI-режиме через Grok API."""
    text = (message.text or "").strip()
    if not text:
        return

    # Команды обрабатывают common-хендлеры (/menu, /start, /restart)
    if text.startswith("/"):
        return

    data = await state.get_data()
    history: list[dict] = list(data.get("ai_history") or [])
    history.append({"role": "user", "content": text})

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        reply = await ask_grok(history, user_id=message.from_user.id)
    except GrokAPIError as e:
        logger.warning("Grok error for user %s: %s", message.from_user.id, e)
        await message.answer(
            f"{AI_UNAVAILABLE}\n\n<i>Техническая информация: {html_escape(str(e))}</i>",
            reply_markup=ai_consultant_keyboard(),
        )
        return

    history.append({"role": "assistant", "content": reply})
    from utils.grok import trim_history
    history = trim_history(history, Config.AI_HISTORY_LIMIT)
    await state.update_data(ai_history=history)

    safe_reply = html_escape(reply)
    chunks = split_message(safe_reply)
    for i, chunk in enumerate(chunks):
        markup = ai_consultant_keyboard() if i == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)


@router.message(AIConsultantState.chatting, F.photo)
async def ai_consultant_photo(message: Message, state: FSMContext) -> None:
    """Анализирует фото кожи через Grok Vision API."""
    user_id = message.from_user.id

    # Проверка лимита фото за сессию
    if not _check_photo_session_limit(user_id):
        await message.answer(
            PHOTO_SESSION_LIMIT,
            reply_markup=ai_consultant_keyboard(),
        )
        return

    # Проверка rate limit
    if not _check_photo_rate(user_id):
        await message.answer(
            PHOTO_RATE_LIMITED,
            reply_markup=ai_consultant_keyboard(),
        )
        return

    # Берём фото максимального размера
    photo: PhotoSize = message.photo[-1]

    # Проверка минимального размера файла (защита от плохого качества)
    if photo.file_size and photo.file_size < PHOTO_MIN_FILE_SIZE:
        await message.answer(
            PHOTO_TOO_SMALL,
            reply_markup=ai_consultant_keyboard(),
        )
        return

    # Проверка минимального разрешения
    if photo.width < PHOTO_MIN_WIDTH or photo.height < PHOTO_MIN_HEIGHT:
        await message.answer(
            PHOTO_TOO_SMALL,
            reply_markup=ai_consultant_keyboard(),
        )
        return

    # Записываем rate limit
    _record_photo(user_id)

    # Показываем "печатает..."
    await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)

    # Скачиваем фото
    try:
        file = await message.bot.get_file(photo.file_id)
        file_bytes_io = await message.bot.download_file(file.file_path)
        image_bytes = file_bytes_io.read() if file_bytes_io is not None else b""
    except Exception as e:
        logger.error("Ошибка скачивания фото от %s: %s", user_id, e)
        await message.answer(
            "Не удалось скачать фото. Попробуйте ещё раз.",
            reply_markup=ai_consultant_keyboard(),
        )
        return

    if not image_bytes:
        await message.answer(
            "Не удалось скачать фото. Попробуйте ещё раз.",
            reply_markup=ai_consultant_keyboard(),
        )
        return

    # Текст подписи к фото (если есть)
    caption = (message.caption or "").strip()

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Отправляем в Grok Vision
    try:
        reply = await ask_grok_vision(image_bytes, caption, user_id=user_id)
    except GrokAPIError as e:
        logger.warning("Grok Vision error for user %s: %s", user_id, e)
        await message.answer(
            f"{AI_UNAVAILABLE}\n\n<i>Техническая информация: {html_escape(str(e))}</i>",
            reply_markup=ai_consultant_keyboard(),
        )
        return

    # Сохраняем в историю
    data = await state.get_data()
    history: list[dict] = list(data.get("ai_history") or [])
    history.append({
        "role": "user",
        "content": f"[Фото отправлено] {caption}" if caption else "[Фото кожи]",
    })
    history.append({"role": "assistant", "content": reply})
    from utils.grok import trim_history
    history = trim_history(history, Config.AI_HISTORY_LIMIT)
    await state.update_data(ai_history=history)

    safe_reply = html_escape(reply)
    chunks = split_message(safe_reply)
    for i, chunk in enumerate(chunks):
        markup = ai_consultant_keyboard() if i == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)


@router.message(AIConsultantState.chatting, F.video)
async def ai_consultant_video(message: Message, state: FSMContext) -> None:
    """Обработка видео: Grok Vision анализирует thumbnail (превью)."""
    video = message.video
    if not video:
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Скачиваем thumbnail (превью видео)
    image_bytes = None
    try:
        if video.thumbnail:
            file = await message.bot.get_file(video.thumbnail.file_id)
            file_bytes_io = await message.bot.download_file(file.file_path)
            image_bytes = file_bytes_io.read() if file_bytes_io else None
    except Exception as e:
        logger.warning("Failed to get video thumbnail for user %s: %s", message.from_user.id, e)

    if not image_bytes:
        await message.answer(
            "📹 Видео получено! Для анализа отправьте фото кожи крупным планом.",
            reply_markup=ai_consultant_keyboard(),
        )
        return

    caption = message.caption or "Опиши что видно на этом кадре из видео"

    try:
        reply = await ask_grok_vision(image_bytes, caption, user_id=message.from_user.id)
    except GrokAPIError as e:
        logger.warning("Grok error for video thumbnail: %s", e)
        await message.answer(
            f"{AI_UNAVAILABLE}\n\n<i>Техническая информация: {html_escape(str(e))}</i>",
            reply_markup=ai_consultant_keyboard(),
            parse_mode="HTML",
        )
        return

    # Сохраняем в историю контекста
    data = await state.get_data()
    history: list[dict] = list(data.get("ai_history") or [])
    history.append({"role": "user", "content": f"[Видео] {caption}"})
    history.append({"role": "assistant", "content": reply})
    from utils.grok import trim_history
    history = trim_history(history, Config.AI_HISTORY_LIMIT)
    await state.update_data(ai_history=history)

    safe_reply = html_escape(reply)
    chunks = split_message(safe_reply)
    for i, chunk in enumerate(chunks):
        markup = ai_consultant_keyboard() if i == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup, parse_mode="HTML")


@router.message(AIConsultantState.chatting, F.voice)
async def ai_consultant_voice(message: Message, state: FSMContext) -> None:
    """Голосовые сообщения — пока не поддерживается, просим текст."""
    await message.answer(
        "🎤 Голосовые сообщения пока не поддерживаются.\n\n"
        "Напишите ваш вопрос текстом или отправьте фото кожи.",
        reply_markup=ai_consultant_keyboard(),
    )


@router.message(AIConsultantState.chatting)
async def ai_consultant_non_text(message: Message) -> None:
    """В AI-режиме принимаются текст, фото, видео и голосовые."""
    content_type = message.content_type or "неизвестный"
    await message.answer(
        f"Принимаются текстовые сообщения, фото, видео и голосовые.\n"
        f"Вы отправили: {content_type}.\n\n"
        f"📸 Пришлите фото кожи или напишите вопрос текстом.",
        reply_markup=ai_consultant_keyboard(),
    )