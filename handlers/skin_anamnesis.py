"""
Подбор ухода за кожей с помощью ИИ — оптимальная версия v3.
11 вопросов, ~5-7 минут, интеграция с Grok Vision.
"""

import json
import logging
from datetime import datetime, timezone
from html import escape as html_escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from database import User
from keyboards import (
    SKIN_AGE_CHOICES,
    SKIN_ALLERGY_CHOICES,
    SKIN_AREAS,
    SKIN_DURATION_CHOICES,
    SKIN_GOALS,
    SKIN_HOME_CARE,
    SKIN_PRO_CARE,
    SKIN_PROBLEMS,
    SKIN_SEASON_CHOICES,
    SKIN_TYPE_CHOICES,
    skin_areas_keyboard,
    skin_continue_keyboard,
    skin_final_keyboard,
    skin_goals_keyboard,
    skin_home_care_keyboard,
    skin_pro_care_keyboard,
    skin_problems_keyboard,
    skin_skip_keyboard,
    skin_start_keyboard,
    skin_why_keyboard,
)
from utils.grok import GrokAPIError, ask_grok_vision
from utils.helpers import get_user_by_telegram_id
from utils.states import SkinAnamnesisState
from utils.text_format import split_message

logger = logging.getLogger(__name__)

router = Router()

TOTAL_QUESTIONS = 11

# Новые клавиатуры для v3 (в keyboards.py не было)
SKIN_INFLAMMATION_CHOICES = [
    ("Да, есть активное воспаление", "yes"),
    ("Нет", "no"),
    ("Было недавно, сейчас лучше", "was"),
]

SKIN_BUDGET_CHOICES = [
    ("Эконом + только домашний уход", "economy"),
    ("Средний бюджет + готов(а) к процедурам", "medium"),
    ("Премиум + хочу комплексный подход", "premium"),
    ("Пока не знаю, хочу варианты", "unsure"),
]


# =============================================================================
# БЕЗОПАСНОЕ ОБНОВЛЕНИЕ КЛАВИАТУРЫ
# =============================================================================

async def _safe_edit_markup(message: Message, markup) -> None:
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as e:
        logger.debug("edit_markup failed (expected on double-tap): %s", e)


def _progress(q: int) -> str:
    pct = round((q - 1) / TOTAL_QUESTIONS * 100)
    return f"📋 <b>Вопрос {q} из {TOTAL_QUESTIONS}</b> • Прогресс: {pct}%\n"


def _skin_inflammation_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, value in SKIN_INFLAMMATION_CHOICES:
        builder.row(InlineKeyboardButton(text=text, callback_data=f"skinflam_{value}"))
    return builder.as_markup()


def _skin_budget_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, value in SKIN_BUDGET_CHOICES:
        builder.row(InlineKeyboardButton(text=text, callback_data=f"skbudget_{value}"))
    return builder.as_markup()


def _skin_comment_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✍️ Написать комментарий", callback_data="skin_comment_write"))
    builder.row(InlineKeyboardButton(text="Пропустить ➡️", callback_data="skin_comment_skip"))
    return builder.as_markup()


def _skin_photo_offer_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📸 Прикрепить фото", callback_data="skin_photo"))
    builder.row(InlineKeyboardButton(text="Пропустить ➡️", callback_data="skin_photo_skip"))
    return builder.as_markup()


def _skin_final_cta_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗓 Записаться к Ашуре", callback_data="menu_booking"))
    builder.row(InlineKeyboardButton(text="💬 Задать вопрос ИИ", callback_data="menu_ai_consultant"))
    builder.row(InlineKeyboardButton(text="🔄 Пройти заново", callback_data="skin_restart"))
    builder.row(InlineKeyboardButton(text="☰ Главное меню", callback_data="menu_main"))
    return builder.as_markup()


# =============================================================================
# НАВИГАЦИЯ
# =============================================================================

async def _ask_question(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    q = data.get("skin_q", 1)

    question_handlers = {
        1: _q1_age, 2: _q2_type, 3: _q3_condition,
        4: _q4_problems, 5: _q5_areas, 6: _q6_duration,
        7: _q7_inflammation, 8: _q8_home_care, 9: _q9_pro_care,
        10: _q10_allergies, 11: _q11_goals_budget,
    }
    handler = question_handlers.get(q)
    if handler:
        await handler(message, state)
    else:
        await _step_comment(message, state)


# =============================================================================
# СТАРТОВЫЙ ЭКРАН
# =============================================================================

@router.callback_query(F.data == "menu_skin_anamnesis")
async def start_skin_anamnesis(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession,
) -> None:
    user = await get_user_by_telegram_id(session, callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройдите регистрацию: /start", show_alert=True)
        return

    data = await state.get_data()
    if data.get("skin_q") and data.get("skin_q") > 1:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="Продолжить ➡️", callback_data="skin_resume"))
        builder.row(InlineKeyboardButton(text="🔄 Начать заново", callback_data="skin_go"))
        builder.row(InlineKeyboardButton(text="☰ Главное меню", callback_data="menu_main"))
        await callback.message.answer(
            "У тебя есть незавершённый анамнез. Продолжить?",
            reply_markup=builder.as_markup(),
        )
    else:
        await _show_start(callback.message)
    await callback.answer()


async def _show_start(message: Message) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Да, поехали! 🚀", callback_data="skin_go"))
    builder.row(InlineKeyboardButton(text="Зачем это нужно?", callback_data="skin_why"))
    await message.answer(
        "Привет, красотка! ✨\n\n"
        "Я — помощник Ашуры. Задам тебе 10–11 коротких вопросов о коже, чтобы подготовить консультацию.\n\n"
        "⚠️ Я не врач. Персональный план и процедуры — только у Ашуры после очной встречи.\n\n"
        "Готовы? 💫",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "skin_why")
async def skin_why(callback: CallbackQuery) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Понятно, начинаем! 🚀", callback_data="skin_go"))
    await callback.message.answer(
        "Анамнез помогает:\n"
        "✓ Сэкономить время на приёме\n"
        "✓ Получить базовые советы прямо сейчас\n"
        "✓ Подготовить персональный план заранее\n\n"
        "Вся информация конфиденциальна.",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "skin_go")
async def skin_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SkinAnamnesisState.in_progress)
    await state.update_data(skin_q=1, skin_answers={}, skin_selected=set())
    await _ask_question(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "skin_resume")
async def skin_resume(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SkinAnamnesisState.in_progress)
    await _ask_question(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "skin_restart")
async def skin_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_start(callback.message)
    await callback.answer()


@router.callback_query(SkinAnamnesisState.in_progress, F.data == "skin_next")
async def skin_next(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    q = data.get("skin_q", 1)
    await state.update_data(skin_q=q + 1, skin_selected=set())
    await _ask_question(callback.message, state)
    await callback.answer()


@router.callback_query(SkinAnamnesisState.in_progress, F.data == "skin_back")
async def skin_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к предыдущему вопросу."""
    data = await state.get_data()
    q = data.get("skin_q", 1)
    if q > 1:
        await state.update_data(skin_q=q - 1, skin_selected=set())
        await _ask_question(callback.message, state)
    else:
        await callback.answer("Это первый вопрос", show_alert=True)
    await callback.answer()


# =============================================================================
# ВОПРОС 1: ВОЗРАСТ
# =============================================================================

async def _q1_age(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{_progress(1)}\nСколько тебе лет?",
        reply_markup=_choice_keyboard(SKIN_AGE_CHOICES, "skage"),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skage_"))
async def handle_age(callback: CallbackQuery, state: FSMContext) -> None:
    age = callback.data.replace("skage_", "")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["age"] = age
    await state.update_data(skin_answers=answers)

    tips = {
        "18-25": "профилактика и базовый уход 🌱",
        "26-35": "антистресс и первые антивозрастные шаги ✨",
        "36-45": "активное anti-age и укрепление 💪",
        "46-55": "плотность, упругость, глубокое питание 🌹",
        "55+": "бережный уход и комфорт 💖",
    }
    await callback.message.answer(
        f"В твоём возрасте главный фокус: {tips.get(age, '')}",
        reply_markup=skin_continue_keyboard(),
    )
    await callback.answer()


# =============================================================================
# ВОПРОС 2: ТИП КОЖИ
# =============================================================================

async def _q2_type(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{_progress(2)}\nКак бы ты описала свою кожу?",
        reply_markup=_choice_keyboard(SKIN_TYPE_CHOICES, "sktype"),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("sktype_"))
async def handle_type(callback: CallbackQuery, state: FSMContext) -> None:
    skin_type = callback.data.replace("sktype_", "")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["skin_type"] = skin_type
    await state.update_data(skin_answers=answers)

    if skin_type == "unknown":
        builder = InlineKeyboardBuilder()
        for text, value in [
            ("Блестит вся 😅", "oily"), ("Блестит Т-зона", "combo"),
            ("Нормальная", "normal"), ("Стянутость, шелушится", "dry"),
            ("Покраснела от воды", "sensitive"),
        ]:
            builder.row(InlineKeyboardButton(text=text, callback_data=f"sktypedet_{value}"))
        await callback.message.answer(
            "Без проблем! Утром после умывания через 2 часа твоя кожа:",
            reply_markup=builder.as_markup(),
        )
    else:
        tips = {
            "oily": "мягкое очищение + лёгкое увлажнение + матирование",
            "dry": "нежное очищение + плотное увлажнение + питание",
            "combo": "баланс: очищение Т-зоны + увлажнение щёк",
            "normal": "поддержание баланса + защита",
            "sensitive": "минимум ингредиентов + успокоение",
        }
        await callback.message.answer(
            f"💡 Для твоего типа кожи: {tips.get(skin_type, '')}",
            reply_markup=skin_continue_keyboard(),
        )
    await callback.answer()


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("sktypedet_"))
async def handle_type_detect(callback: CallbackQuery, state: FSMContext) -> None:
    detected = callback.data.replace("sktypedet_", "")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["skin_type"] = detected
    await state.update_data(skin_answers=answers)
    await callback.message.answer("Определили!", reply_markup=skin_continue_keyboard())
    await callback.answer()


# =============================================================================
# ВОПРОС 3: ТЕКУЩЕЕ СОСТОЯНИЕ
# =============================================================================

async def _q3_condition(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{_progress(3)}\nСейчас кожа ведёт себя как?",
        reply_markup=_choice_keyboard(SKIN_SEASON_CHOICES, "skseason"),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skseason_"))
async def handle_season(callback: CallbackQuery, state: FSMContext) -> None:
    season = callback.data.replace("skseason_", "")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["skin_condition"] = season
    await state.update_data(skin_answers=answers)

    if season in ("worse", "crisis"):
        await callback.message.answer(
            "Поняла тебя! Запишем всё подробно. Большинство проблем решаемы 💪"
        )
    await callback.message.answer("Двигаемся дальше ➡️", reply_markup=skin_continue_keyboard())
    await callback.answer()


# =============================================================================
# ВОПРОС 4: ПРОБЛЕМЫ (множественный)
# =============================================================================

async def _q4_problems(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("skin_selected", set())
    await message.answer(
        f"{_progress(4)}\nКакие проблемы беспокоят прямо сейчас?\n(можно несколько)",
        reply_markup=skin_problems_keyboard(selected),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skprob_"))
async def handle_problems(callback: CallbackQuery, state: FSMContext) -> None:
    val = callback.data.replace("skprob_", "")
    data = await state.get_data()
    selected = set(data.get("skin_selected", set()))

    if val == "done":
        if not selected:
            await callback.answer("Выбери хотя бы один вариант", show_alert=True)
            return
        answers = data.get("skin_answers", {})
        answers["problems"] = list(selected)
        await state.update_data(skin_answers=answers, skin_selected=set())
        await callback.message.answer(
            f"Зафиксировано: {len(selected)} проблем. Переходим к локализации ➡️"
        )
        await state.update_data(skin_q=5)
        await _ask_question(callback.message, state)
    else:
        if val == "none":
            selected = {"none"}
        elif "none" in selected:
            selected.discard("none")
            selected.add(val)
        else:
            selected.discard(val) if val in selected else selected.add(val)
        await state.update_data(skin_selected=selected)
        await _safe_edit_markup(callback.message, skin_problems_keyboard(selected))
    await callback.answer()


# =============================================================================
# ВОПРОС 5: ЛОКАЛИЗАЦИЯ (множественный)
# =============================================================================

async def _q5_areas(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("skin_selected", set())
    await message.answer(
        f"{_progress(5)}\nГде именно проявляются проблемы? 📍",
        reply_markup=skin_areas_keyboard(selected),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skarea_"))
async def handle_areas(callback: CallbackQuery, state: FSMContext) -> None:
    val = callback.data.replace("skarea_", "")
    data = await state.get_data()
    selected = set(data.get("skin_selected", set()))

    if val == "done":
        if not selected:
            await callback.answer("Выбери хотя бы один вариант", show_alert=True)
            return
        answers = data.get("skin_answers", {})
        answers["problem_areas"] = list(selected)
        await state.update_data(skin_answers=answers, skin_selected=set())
        await callback.message.answer("Отлично! Теперь уточним давность ➡️")
        await state.update_data(skin_q=6)
        await _ask_question(callback.message, state)
    else:
        selected.discard(val) if val in selected else selected.add(val)
        await state.update_data(skin_selected=selected)
        await _safe_edit_markup(callback.message, skin_areas_keyboard(selected))
    await callback.answer()


# =============================================================================
# ВОПРОС 6: ДЛИТЕЛЬНОСТЬ
# =============================================================================

async def _q6_duration(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{_progress(6)}\nКак давно эти проблемы с тобой? ⏰",
        reply_markup=_choice_keyboard(SKIN_DURATION_CHOICES, "skdur"),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skdur_"))
async def handle_duration(callback: CallbackQuery, state: FSMContext) -> None:
    dur = callback.data.replace("skdur_", "")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["duration"] = dur
    await state.update_data(skin_answers=answers)

    tips = {
        "month": "Возможно, реакция на стресс или новый продукт. Попробуй исключить новинки на 2 недели.",
        "3months": "Советую обратить внимание — записаться к Ашуре для диагностики.",
        "year": "Больше года — это сигнал обратиться к специалисту.",
        "over_year": "Давно пора к специалисту! Ашура найдёт причину.",
        "always": "Возможно, особенность типа кожи. Ашура поможет подобрать уход.",
    }
    await callback.message.answer(f"💡 {tips.get(dur, '')}", reply_markup=skin_continue_keyboard())
    await callback.answer()


# =============================================================================
# ВОПРОС 7: АКТИВНОЕ ВОСПАЛЕНИЕ
# =============================================================================

async def _q7_inflammation(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{_progress(7)}\nЕсть ли сейчас активные воспаления, гнойнички или сильное покраснение?",
        reply_markup=_skin_inflammation_keyboard(),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skinflam_"))
async def handle_inflammation(callback: CallbackQuery, state: FSMContext) -> None:
    val = callback.data.replace("skinflam_", "")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["inflammation"] = val
    await state.update_data(skin_answers=answers)

    if val == "yes":
        await callback.message.answer(
            "Поняла. В таких случаях Ашура сначала снимает воспаление. Это важно для безопасности."
        )
    await callback.message.answer("Переходим к уходу ➡️", reply_markup=skin_continue_keyboard())
    await callback.answer()


# =============================================================================
# ВОПРОС 8: ДОМАШНИЙ УХОД (множественный)
# =============================================================================

async def _q8_home_care(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("skin_selected", set())
    await message.answer(
        f"{_progress(8)}\nЧто используешь ежедневно из ухода?\n(можно несколько)",
        reply_markup=skin_home_care_keyboard(selected),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skcare_"))
async def handle_home_care(callback: CallbackQuery, state: FSMContext) -> None:
    val = callback.data.replace("skcare_", "")
    data = await state.get_data()
    selected = set(data.get("skin_selected", set()))

    if val == "write":
        await state.set_state(SkinAnamnesisState.waiting_text)
        await state.update_data(skin_text_field="home_care_text")
        await callback.message.answer("✏️ Напиши, чем пользуешься:", reply_markup=skin_skip_keyboard())
        await callback.answer()
        return

    if val == "done":
        if not selected:
            await callback.answer("Выбери хотя бы один вариант", show_alert=True)
            return
        answers = data.get("skin_answers", {})
        answers["home_care"] = list(selected)
        await state.update_data(skin_answers=answers, skin_selected=set())
        if "nothing" in selected:
            await callback.message.answer(
                "Ого! Даже базовый уход из 3 шагов творит чудеса:\n"
                "1. Очищение\n2. Увлажнение\n3. SPF днём"
            )
        await callback.message.answer("Переходим к процедурам ➡️")
        await state.update_data(skin_q=9)
        await _ask_question(callback.message, state)
    else:
        if val == "nothing":
            selected = {"nothing"}
        elif "nothing" in selected:
            selected.discard("nothing")
            selected.add(val)
        else:
            selected.discard(val) if val in selected else selected.add(val)
        await state.update_data(skin_selected=selected)
        await _safe_edit_markup(callback.message, skin_home_care_keyboard(selected))
    await callback.answer()


# =============================================================================
# ВОПРОС 9: ПРОФЕССИОНАЛЬНЫЕ ПРОЦЕДУРЫ (множественный)
# =============================================================================

async def _q9_pro_care(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("skin_selected", set())
    await message.answer(
        f"{_progress(9)}\nКакие профессиональные процедуры уже пробовала?\n(можно несколько)",
        reply_markup=skin_pro_care_keyboard(selected),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skpro_"))
async def handle_pro_care(callback: CallbackQuery, state: FSMContext) -> None:
    val = callback.data.replace("skpro_", "")
    data = await state.get_data()
    selected = set(data.get("skin_selected", set()))

    if val == "write":
        await state.set_state(SkinAnamnesisState.waiting_text)
        await state.update_data(skin_text_field="pro_care_text")
        await callback.message.answer("✏️ Напиши, какие процедуры пробовала:", reply_markup=skin_skip_keyboard())
        await callback.answer()
        return

    if val == "done":
        if not selected:
            await callback.answer("Выбери хотя бы один вариант", show_alert=True)
            return
        answers = data.get("skin_answers", {})
        answers["pro_care"] = list(selected)
        await state.update_data(skin_answers=answers, skin_selected=set())
        await callback.message.answer("Переходим к аллергиям ➡️")
        await state.update_data(skin_q=10)
        await _ask_question(callback.message, state)
    else:
        if val == "nothing":
            selected = {"nothing"}
        elif "nothing" in selected:
            selected.discard("nothing")
            selected.add(val)
        else:
            selected.discard(val) if val in selected else selected.add(val)
        await state.update_data(skin_selected=selected)
        await _safe_edit_markup(callback.message, skin_pro_care_keyboard(selected))
    await callback.answer()


# =============================================================================
# ВОПРОС 10: АЛЛЕРГИИ И ЗДОРОВЬЕ
# =============================================================================

async def _q10_allergies(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{_progress(10)}\nЕсть ли аллергия на косметику или особенности здоровья, которые влияют на кожу?",
        reply_markup=_choice_keyboard(SKIN_ALLERGY_CHOICES, "skallergy"),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skallergy_"))
async def handle_allergy(callback: CallbackQuery, state: FSMContext) -> None:
    allergy = callback.data.replace("skallergy_", "")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["allergies"] = allergy
    await state.update_data(skin_answers=answers)

    if allergy == "yes_known":
        await state.set_state(SkinAnamnesisState.waiting_text)
        await state.update_data(skin_text_field="allergy_details")
        await callback.message.answer(
            "Напиши, на что именно аллергия:", reply_markup=skin_skip_keyboard()
        )
    else:
        await callback.message.answer("Переходим к целям ➡️", reply_markup=skin_continue_keyboard())
    await callback.answer()


# =============================================================================
# ВОПРОС 11: ЦЕЛИ + БЮДЖЕТ (объединено)
# =============================================================================

async def _q11_goals_budget(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("skin_selected", set())
    await message.answer(
        f"{_progress(11)}\nЧего хочешь добиться?\n(можно несколько)",
        reply_markup=skin_goals_keyboard(selected),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skgoal_"))
async def handle_goals(callback: CallbackQuery, state: FSMContext) -> None:
    val = callback.data.replace("skgoal_", "")
    data = await state.get_data()
    selected = set(data.get("skin_selected", set()))

    if val == "done":
        if not selected:
            await callback.answer("Выбери хотя бы один вариант", show_alert=True)
            return
        answers = data.get("skin_answers", {})
        answers["goals"] = list(selected)
        await state.update_data(skin_answers=answers, skin_selected=set())
        # Сразу к бюджету
        await message_answer_budget(callback.message)
    else:
        selected.discard(val) if val in selected else selected.add(val)
        await state.update_data(skin_selected=selected)
        await _safe_edit_markup(callback.message, skin_goals_keyboard(selected))
    await callback.answer()


async def message_answer_budget(message: Message) -> None:
    await message.answer(
        "Какой у тебя бюджет и готовность к процедурам?",
        reply_markup=_skin_budget_keyboard(),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data.startswith("skbudget_"))
async def handle_budget(callback: CallbackQuery, state: FSMContext) -> None:
    budget = callback.data.replace("skbudget_", "")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["budget"] = budget
    await state.update_data(skin_answers=answers, skin_q=12)

    # Переход к опциональному комментарию
    await _step_comment(callback.message, state)
    await callback.answer()


# =============================================================================
# ОПЦИОНАЛЬНЫЙ КОММЕНТАРИЙ
# =============================================================================

async def _step_comment(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Хочешь что-то дополнительно сказать или уточнить?\n"
        "(например: \"боюсь боли\", \"хочу быстрое решение\", \"были неудачные процедуры раньше\")",
        reply_markup=_skin_comment_keyboard(),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data == "skin_comment_write")
async def handle_comment_write(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SkinAnamnesisState.waiting_text)
    await state.update_data(skin_text_field="comment")
    await callback.message.answer("✍️ Напиши свой комментарий:", reply_markup=skin_skip_keyboard())
    await callback.answer()


@router.callback_query(SkinAnamnesisState.in_progress, F.data == "skin_comment_skip")
async def handle_comment_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await _step_photo(callback.message, state)
    await callback.answer()


# =============================================================================
# ОПЦИОНАЛЬНОЕ ФОТО
# =============================================================================

async def _step_photo(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Хочешь прикрепить 1–2 фото кожи при дневном свете?\n"
        "Это поможет Ашуре лучше подготовиться (по желанию).",
        reply_markup=_skin_photo_offer_keyboard(),
    )


@router.callback_query(SkinAnamnesisState.in_progress, F.data == "skin_photo")
async def handle_photo_offer(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SkinAnamnesisState.waiting_photo)
    await callback.message.answer("📸 Пришли фото лица или зоны декольте.")
    await callback.answer()


@router.callback_query(SkinAnamnesisState.in_progress, F.data == "skin_photo_skip")
async def handle_photo_skip(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    # Guard: если уже финализируем — игнорируем повторный клик
    data = await state.get_data()
    if data.get("_skin_finalizing"):
        await callback.answer()
        return
    await state.update_data(_skin_finalizing=True)
    await callback.answer()
    try:
        await _show_final(callback.message, state, session, callback.from_user.id)
    finally:
        await state.update_data(_skin_finalizing=False)


@router.message(SkinAnamnesisState.waiting_photo, F.photo)
async def handle_photo_receive(message: Message, state: FSMContext, session: AsyncSession) -> None:
    photo = message.photo[-1]
    if photo.file_size and photo.file_size < 30000:
        await message.answer("📏 Фото слишком маленькое. Сделай чёткое фото.")
        return

    await message.answer("🔍 Анализирую фото... (это может занять 10-20 секунд)")

    try:
        file = await message.bot.get_file(photo.file_id)
        file_bytes_io = await message.bot.download_file(file.file_path)
        image_bytes = file_bytes_io.read() if file_bytes_io is not None else b""
    except Exception as e:
        logger.error("Ошибка скачивания фото: %s", e)
        image_bytes = b""

    if not image_bytes:
        await message.answer("Не удалось скачать фото. Продолжим без анализа.")
        await _show_final(message, state, session, message.from_user.id)
        return

    # Сохраняем фото для отправки админу (до анализа)
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["photo_file_id"] = photo.file_id
    await state.update_data(skin_answers=answers)

    try:
        # Анонимизируем: не передаём имя/телефон в LLM
        reply = await ask_grok_vision(image_bytes, "Проанализируй состояние кожи. Дай краткие рекомендации по уходу.", user_id=message.from_user.id)
    except GrokAPIError as e:
        logger.warning("Grok Vision error: %s", e)
        reply = ""

    if reply:
        answers = (await state.get_data()).get("skin_answers", {})
        answers["ai_skin_analysis"] = reply
        await state.update_data(skin_answers=answers)
        for chunk in split_message(html_escape(reply)):
            await message.answer(chunk)
    else:
        await message.answer("Не удалось проанализировать фото. Продолжим без анализа.")

    await _show_final(message, state, session, message.from_user.id)


@router.message(SkinAnamnesisState.waiting_photo, F.video)
async def handle_video_receive(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Обработка видео в анамнезе."""
    await message.answer("📹 Видео получено! Сохраняю...")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["has_video"] = True
    if message.video:
        answers["video_file_id"] = message.video.file_id
    await state.update_data(skin_answers=answers)
    await message.answer("📹 Видео сохранено. Ашура посмотрит его при подготовке к приёму.")
    await _show_final(message, state, session, message.from_user.id)


@router.message(SkinAnamnesisState.waiting_photo, F.voice)
async def handle_voice_receive(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Обработка голосового в анамнезе."""
    await message.answer("🎤 Голосовое получено! Сохраняю...")
    data = await state.get_data()
    answers = data.get("skin_answers", {})
    answers["has_voice"] = True
    if message.voice:
        answers["voice_file_id"] = message.voice.file_id
    await state.update_data(skin_answers=answers)
    await message.answer("🎤 Голосовое сохранено. Ашура прослушает его при подготовке к приёму.")
    await _show_final(message, state, session, message.from_user.id)


@router.message(SkinAnamnesisState.waiting_photo)
async def handle_photo_wrong_type(message: Message) -> None:
    await message.answer("📸 Пришли фото, видео или голосовое сообщение.")


# =============================================================================
# ТЕКСТОВЫЙ ВВОД (аллергии, уход, комментарий)
# =============================================================================

@router.callback_query(SkinAnamnesisState.waiting_text, F.data == "skin_skip")
async def handle_skip_text(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SkinAnamnesisState.in_progress)
    data = await state.get_data()
    field = data.get("skin_text_field")

    if field == "allergy_details":
        await callback.message.answer("Переходим к целям ➡️", reply_markup=skin_continue_keyboard())
    elif field == "home_care_text":
        await callback.message.answer("Переходим к процедурам ➡️")
        await state.update_data(skin_q=9)
        await _ask_question(callback.message, state)
    elif field == "pro_care_text":
        await callback.message.answer("Переходим к аллергиям ➡️")
        await state.update_data(skin_q=10)
        await _ask_question(callback.message, state)
    elif field == "comment":
        await _step_photo(callback.message, state)
    await callback.answer()


@router.message(SkinAnamnesisState.waiting_text, F.text)
async def handle_text_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("skin_text_field")
    answers = data.get("skin_answers", {})
    text = message.text.strip()

    if field == "allergy_details":
        answers["allergy_details"] = text
        await state.update_data(skin_answers=answers)
        await state.set_state(SkinAnamnesisState.in_progress)
        await message.answer("Переходим к целям ➡️", reply_markup=skin_continue_keyboard())
    elif field == "home_care_text":
        answers["home_care_text"] = text
        await state.update_data(skin_answers=answers, skin_q=9, skin_selected=set())
        await state.set_state(SkinAnamnesisState.in_progress)
        await message.answer("✅ Записала! Переходим к процедурам ➡️")
        await _ask_question(message, state)
    elif field == "pro_care_text":
        answers["pro_care_text"] = text
        await state.update_data(skin_answers=answers, skin_q=10, skin_selected=set())
        await state.set_state(SkinAnamnesisState.in_progress)
        await message.answer("✅ Записала! Переходим к аллергиям ➡️")
        await _ask_question(message, state)
    elif field == "comment":
        answers["comment"] = text
        await state.update_data(skin_answers=answers)
        await state.set_state(SkinAnamnesisState.in_progress)
        await message.answer("✅ Комментарий записан!")
        await _step_photo(message, state)


# =============================================================================
# ВСПОМОГАТЕЛЬНАЯ КЛАВИАТУРА
# =============================================================================

def _choice_keyboard(choices: list[tuple[str, str]], prefix: str):
    builder = InlineKeyboardBuilder()
    for text, value in choices:
        builder.row(InlineKeyboardButton(text=text, callback_data=f"{prefix}_{value}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="skin_back"))
    return builder.as_markup()


# =============================================================================
# ФИНАЛ
# =============================================================================

async def _show_final(message: Message, state: FSMContext, session: AsyncSession, telegram_id: int = 0) -> None:
    data = await state.get_data()
    answers = data.get("skin_answers", {})

    report = _build_report(answers)
    await message.answer(report, parse_mode="HTML")

    await message.answer("⏳ Генерирую рекомендации от ИИ...")

    # Один вызов Grok (подробные рекомендации) — используем и для клиента, и для админа
    recommendations = None
    try:
        recommendations = await _generate_recommendations(answers, for_admin=False, user_id=telegram_id)
    except Exception as e:
        logger.warning("_show_final: Grok failed: %s", e)

    if recommendations:
        await message.answer("💡 <b>Рекомендации от ИИ:</b>", parse_mode="HTML")
        for chunk in split_message(html_escape(recommendations)):
            await message.answer(chunk)
    else:
        await message.answer("⚠️ Не удалось сгенерировать рекомендации. Ашура подготовит их лично.")

    await message.answer(
        "Отлично! Ашура уже готовит рекомендации под тебя.\n\n"
        "На приёме она уже будет знать о твоей коже — это сэкономит время!",
        reply_markup=_skin_final_cta_keyboard(),
    )

    # ВСЕГДА сохраняем и уведомляем — даже если Grok упал
    await _save_and_notify(message, state, answers, session, telegram_id, ai_recommendations=recommendations)


def _build_report(answers: dict) -> str:
    type_names = {
        "oily": "Жирная", "dry": "Сухая", "combo": "Комбинированная",
        "normal": "Нормальная", "sensitive": "Чувствительная",
    }
    condition_names = {
        "better": "Лучше обычного", "same": "Как всегда",
        "worse": "Хуже обычного", "crisis": "Катастрофа",
    }
    dur_names = {
        "month": "Менее месяца", "3months": "1-3 месяца",
        "year": "3-12 месяцев", "over_year": "Больше года", "always": "Всегда",
    }
    problem_names = {
        "acne": "Акне / высыпания", "comedones": "Чёрные точки / поры",
        "postacne": "Постакне / пятна", "redness": "Покраснения / купероз",
        "sagging": "Потеря упругости / овал", "wrinkles": "Морщины",
        "dehydration": "Обезвоживание / шелушения", "dullness": "Тусклый цвет лица",
        "dark_circles": "Тёмные круги / мешки", "pigmentation": "Пигментные пятна",
        "oiliness": "Жирный блеск", "sensitivity": "Повышенная чувствительность",
        "none": "Нет проблем",
    }
    infl_names = {"yes": "Да, активное воспаление", "no": "Нет", "was": "Было недавно, сейчас лучше"}
    allergy_names = {
        "yes_known": "Есть аллергия (указала)", "no": "Нет аллергии",
        "unknown": "Не знает", "reactive": "Кожа часто реагирует покраснением",
    }
    care_names = {
        "cleanser": "Очищение", "toner": "Тоник / сыворотка", "serum": "Сыворотка",
        "day_cream": "Крем дневной", "night_cream": "Крем ночной",
        "eye_cream": "Крем для глаз", "spf_cream": "SPF", "masks": "Маски",
        "peeling": "Пилинг / скраб", "nothing": "Ничего не использует",
    }
    pro_names = {
        "facial": "Чистка лица", "peeling": "Пилинги", "hardware": "Аппаратные процедуры",
        "injections": "Инъекции (ботокс/филлеры)", "laser": "Лазер",
        "bior": "Биоревитализация", "meso": "Мезотерапия", "nothing": "Ничего не пробовала",
    }
    goal_names = {
        "clear": "Чистая кожа без высыпаний", "glow": "Увлажнение и сияние",
        "anti_wrinkle": "Уменьшение морщин", "lifting": "Подтяжка овала",
        "depigment": "Уменьшение пигментации", "pores": "Сужение пор",
        "matte": "Меньше жирного блеска", "no_postacne": "Убрать постакне",
        "prevention": "Профилактика старения", "proper_care": "Правильный уход",
        "trust_ashura": "Довериться Ашуре",
    }
    budget_names = {
        "economy": "Эконом + только домашний уход",
        "medium": "Средний бюджет + готов(а) к процедурам",
        "premium": "Премиум + комплексный подход",
        "unsure": "Пока не знает, хочет варианты",
    }

    def _list(names: dict, values: list) -> str:
        return ", ".join(names.get(v, v) for v in values) if values else "—"

    lines = [
        "✅ <b>АНАМНЕЗ СОБРАН!</b>\n",
        "📋 <b>ТВОЙ ПРОФИЛЬ:</b>",
        f"• Возраст: {answers.get('age', '—')}",
        f"• Тип кожи: {type_names.get(answers.get('skin_type', ''), answers.get('skin_type', '—'))}",
        f"• Состояние: {condition_names.get(answers.get('skin_condition', ''), '—')}",
        f"• Проблемы: {_list(problem_names, answers.get('problems', []))}",
        f"• Зоны: {', '.join(answers.get('problem_areas', [])) or '—'}",
        f"• Давность: {dur_names.get(answers.get('duration', ''), '—')}",
        f"• Воспаление: {infl_names.get(answers.get('inflammation', ''), '—')}",
        f"• Домашний уход: {_list(care_names, answers.get('home_care', []))}",
        f"• Процедуры: {_list(pro_names, answers.get('pro_care', []))}",
        f"• Аллергии: {allergy_names.get(answers.get('allergies', ''), '—')}",
        f"• Цели: {_list(goal_names, answers.get('goals', []))}",
        f"• Бюджет: {budget_names.get(answers.get('budget', ''), '—')}",
    ]
    if answers.get("comment"):
        lines.append(f"• Комментарий: {html_escape(answers['comment'])}")
    return "\n".join(lines)


def _anonymize_for_llm(answers: dict) -> dict:
    """Удаляет персональные данные перед отправкой в LLM (152-ФЗ)."""
    anonymized = dict(answers)
    # Удаляем идентифицирующую информацию
    for key in ('name', 'phone', 'telegram_id', 'username', 'photo_file_id'):
        anonymized.pop(key, None)
    return anonymized


async def _generate_recommendations(answers: dict, for_admin: bool = False, user_id: int = 0) -> str:
    """Генерирует рекомендации через Grok. for_admin=True — подробные, для клиента — краткие."""
    from utils.grok import ask_grok
    # Анонимизируем данные перед отправкой в LLM (152-ФЗ)
    answers = _anonymize_for_llm(answers)

    type_names = {
        "oily": "Жирная", "dry": "Сухая", "combo": "Комбинированная",
        "normal": "Нормальная", "sensitive": "Чувствительная",
    }
    problem_names = {
        "acne": "Акне", "comedones": "Чёрные точки", "postacne": "Постакне",
        "redness": "Покраснения", "sagging": "Потеря упругости", "wrinkles": "Морщины",
        "dehydration": "Обезвоживание", "dullness": "Тусклый цвет", "dark_circles": "Круги под глазами",
        "pigmentation": "Пигментация", "oiliness": "Жирный блеск", "sensitivity": "Чувствительность",
    }

    condition_names = {
        "better": "Лучше обычного", "same": "Как всегда",
        "worse": "Хуже обычного", "crisis": "Катастрофа",
    }
    dur_names = {
        "month": "Менее месяца", "3months": "1-3 месяца",
        "year": "3-12 месяцев", "over_year": "Больше года", "always": "Всегда",
    }
    infl_names = {"yes": "Да, активное", "no": "Нет", "was": "Было недавно"}
    allergy_names = {
        "yes_known": "Есть аллергия", "no": "Нет аллергии",
        "unknown": "Не знает", "reactive": "Кожа реагирует покраснением",
    }
    care_names = {
        "cleanser": "Очищение", "toner": "Тоник/сыворотка", "serum": "Сыворотка",
        "day_cream": "Крем дневной", "night_cream": "Крем ночной",
        "eye_cream": "Крем для глаз", "spf_cream": "SPF", "masks": "Маски",
        "peeling": "Пилинг/скраб", "nothing": "Ничего не использует",
    }
    pro_names = {
        "facial": "Чистка лица", "peeling": "Пилинги", "hardware": "Аппаратные процедуры",
        "injections": "Инъекции", "laser": "Лазер",
        "bior": "Биоревитализация", "meso": "Мезотерапия", "nothing": "Ничего не пробовала",
    }
    goal_names = {
        "clear": "Чистая кожа", "glow": "Увлажнение и сияние",
        "anti_wrinkle": "Уменьшение морщин", "lifting": "Подтяжка овала",
        "depigment": "Уменьшение пигментации", "pores": "Сужение пор",
        "matte": "Меньше жирного блеска", "no_postacne": "Убрать постакне",
        "prevention": "Профилактика старения", "proper_care": "Правильный уход",
        "trust_ashura": "Довериться Ашуре",
    }
    budget_names = {
        "economy": "Эконом, только домашний уход",
        "medium": "Средний бюджет, готова к процедурам",
        "premium": "Премиум, комплексный подход",
        "unsure": "Пока не знает, хочет варианты",
    }

    def _tr(names: dict, val: str) -> str:
        return names.get(val, val)
    def _tr_list(names: dict, vals: list) -> str:
        return ", ".join(names.get(v, v) for v in vals) if vals else ""

    parts = []
    if answers.get("age"):
        parts.append(f"Возраст: {answers['age']}")
    if answers.get("skin_type"):
        parts.append(f"Тип кожи: {_tr(type_names, answers['skin_type'])}")
    if answers.get("skin_condition"):
        parts.append(f"Состояние кожи: {_tr(condition_names, answers['skin_condition'])}")
    if answers.get("problems"):
        parts.append(f"Проблемы: {_tr_list(problem_names, answers['problems'])}")
    if answers.get("problem_areas"):
        parts.append(f"Зоны: {', '.join(answers['problem_areas'])}")
    if answers.get("duration"):
        parts.append(f"Давность проблемы: {_tr(dur_names, answers['duration'])}")
    if answers.get("inflammation"):
        parts.append(f"Активное воспаление: {_tr(infl_names, answers['inflammation'])}")
    if answers.get("home_care"):
        parts.append(f"Домашний уход: {_tr_list(care_names, answers['home_care'])}")
    if answers.get("pro_care"):
        parts.append(f"Профессиональные процедуры: {_tr_list(pro_names, answers['pro_care'])}")
    if answers.get("allergies"):
        parts.append(f"Аллергии: {_tr(allergy_names, answers['allergies'])}")
    if answers.get("allergy_details"):
        parts.append(f"Детали аллергий: {answers['allergy_details']}")
    if answers.get("goals"):
        parts.append(f"Цели клиента: {_tr_list(goal_names, answers['goals'])}")
    if answers.get("budget"):
        parts.append(f"Бюджет: {_tr(budget_names, answers['budget'])}")
    if answers.get("comment"):
        parts.append(f"Комментарий клиента: {answers['comment']}")

    data_text = "\n".join(parts)

    if for_admin:
        prompt = (
            "Я косметолог-дерматолог. Мне нужна помощь в подготовке к приёму клиента. "
            "Я сама принимаю решения и несу ответственность — мне нужен готовый рабочий план, "
            "чтобы экономить время на рутине. Вот анамнез клиента:\n\n"
            f"{data_text}\n\n"
            "Составь мне готовый план работы с этим клиентом:\n\n"
            "1. Предварительная оценка состояния кожи\n"
            "2. Рекомендуемые процедуры с приоритетом (обязательные / рекомендуемые / по желанию)\n"
            "3. Домашний уход: утро, вечер, еженедельно (конкретные ингредиенты и средства)\n"
            "4. Противопоказания и ограничения (аллергии, воспаления)\n"
            "5. Поэтапный план на 2-3 месяца\n"
            "6. Что уточнить у клиента на очном приёме\n\n"
            "Отвечай конкретно и практично на русском языке."
        )
    else:
        prompt = (
            "Ты — виртуальный помощник косметолога Ашуры. На основе данных анамнеза дай 3-4 коротких "
            "рекомендации по уходу за кожей. Не ставь диагнозы. Не назначай конкретные процедуры или препараты. "
            "Всегда рекомендуй записаться к Ашуре для персонального плана. "
            "Отвечай на русском, кратко, дружелюбно.\n\n"
            f"Данные клиента:\n{data_text}"
        )

    try:
        import asyncio
        if for_admin:
            # Свой системный промпт для админа — без конфликтов
            admin_system = (
                "Ты — опытный помощник косметолога. "
                "Твоя задача — помочь косметологу подготовиться к приёму клиента, "
                "составив план процедур и рекомендаций на основе анамнеза. "
                "Отвечай на русском языке, конкретно и структурированно."
            )
            return await asyncio.wait_for(
                ask_grok([{"role": "user", "content": prompt}], system_prompt=admin_system, user_id=user_id),
                timeout=45,
            )
        else:
            return await asyncio.wait_for(
                ask_grok([{"role": "user", "content": prompt}], user_id=user_id),
                timeout=45,
            )
    except Exception as e:
        logger.warning("skin_anamnesis: ошибка Grok API (%s): %s", "админ" if for_admin else "клиент", e)
        return ""


def _build_admin_report(user, answers: dict) -> str:
    """Компактный отчёт анамнеза для админа — только важное, с визуальными маркерами."""
    type_names = {
        "oily": "Жирная", "dry": "Сухая", "combo": "Комби",
        "normal": "Нормальная", "sensitive": "Чувствит.",
    }
    condition_icons = {
        "better": "✅", "same": "➖", "worse": "⚠️", "crisis": "🔴",
    }
    dur_names = {
        "month": "<1 мес", "3months": "1-3 мес", "year": "3-12 мес",
        "over_year": ">1 года", "always": "Всегда",
    }
    infl_icons = {"yes": "🔴 Да", "no": "✅ Нет", "was": "⚠️ Было"}
    allergy_icons = {
        "yes_known": "⚠️ Да", "no": "✅ Нет", "unknown": "❓ Не знает",
        "reactive": "⚠️ Реактивная",
    }
    budget_names = {
        "economy": "💰 Эконом", "medium": "💰💰 Средний",
        "premium": "💰💰💰 Премиум", "unsure": "❓ Не знает",
    }
    problem_short = {
        "acne": "Акне", "comedones": "Поры", "postacne": "Постакне",
        "redness": "Купероз", "sagging": "Упругость", "wrinkles": "Морщины",
        "dehydration": "Обезвож.", "dullness": "Тусклый тон",
        "dark_circles": "Круги", "pigmentation": "Пигмент.",
        "oiliness": "Жирность", "sensitivity": "Чувствит.", "none": "Нет",
    }

    # Тип + состояние
    skin_type = type_names.get(answers.get("skin_type", ""), answers.get("skin_type", "—"))
    condition = answers.get("skin_condition", "")
    cond_icon = condition_icons.get(condition, "—")

    # Проблемы (сокращённо)
    problems = answers.get("problems", [])
    if problems and "none" not in problems:
        prob_text = ", ".join(problem_short.get(p, p) for p in problems[:5])
        if len(problems) > 5:
            prob_text += f" +{len(problems) - 5}"
        prob_line = f"🔴 {prob_text}"
    else:
        prob_line = "✅ Нет проблем"

    # Зоны
    areas = answers.get("problem_areas", [])
    areas_line = f"📍 {', '.join(areas)}" if areas else ""

    # Давность
    duration = dur_names.get(answers.get("duration", ""), "—")

    # Воспаление
    inflammation = infl_icons.get(answers.get("inflammation", ""), "—")

    # Аллергии
    allergies = allergy_icons.get(answers.get("allergies", ""), "—")

    # Уход (только если есть)
    home_care = answers.get("home_care", [])
    if home_care and "nothing" not in home_care:
        care_count = len(home_care)
        care_line = f"🧴 Уход: {care_count} средств"
    elif "nothing" in home_care:
        care_line = "🧴 Уход: ❌ Нет"
    else:
        care_line = ""

    # Процедуры
    pro_care = answers.get("pro_care", [])
    if pro_care and "nothing" not in pro_care:
        pro_line = f"💉 Процедур: {len(pro_care)}"
    elif "nothing" in pro_care:
        pro_line = "💉 Процедур: не было"
    else:
        pro_line = ""

    # Цели
    goal_short = {
        "clear": "Чистая кожа", "glow": "Сияние", "anti_wrinkle": "Антивозраст",
        "lifting": "Лифтинг", "depigment": "От пигмент.", "pores": "Поры",
        "matte": "Матовость", "no_postacne": "Постакне", "prevention": "Профилактика",
        "proper_care": "Уход", "trust_ashura": "Доверяет",
    }
    goals = answers.get("goals", [])
    if goals:
        goals_text = ", ".join(goal_short.get(g, g) for g in goals[:4])
        if len(goals) > 4:
            goals_text += f" +{len(goals) - 4}"
    else:
        goals_text = "—"

    # Бюджет
    budget = budget_names.get(answers.get("budget", ""), "—")

    # Комментарий (только если есть)
    comment = answers.get("comment", "")
    comment_line = f"\n💬 <i>{html_escape(comment)}</i>" if comment else ""

    # Собираем
    lines = [
        f"✨ <b>АНАМНЕЗ КОЖИ</b>",
        f"👤 {html_escape(user.name)} | 📞 {html_escape(user.phone)} | 🆔 <code>{user.telegram_id}</code>",
        "",
        f"👩 {skin_type} | {cond_icon} {condition}",
        f"⏰ Давность: {duration}",
        f"🌡️ Воспаление: {inflammation}",
        f"💊 Аллергии: {allergies}",
        "",
        prob_line,
    ]
    if areas_line:
        lines.append(areas_line)
    lines.append("")
    lines.append(f"🎯 Цели: {goals_text}")
    lines.append(f"💰 Бюджет: {budget}")
    if care_line:
        lines.append(care_line)
    if pro_line:
        lines.append(pro_line)
    if comment_line:
        lines.append(comment_line)

    return "\n".join(lines)


async def _save_and_notify(message: Message, state: FSMContext, answers: dict, session: AsyncSession, telegram_id: int = 0, ai_recommendations: str | None = None) -> None:
    if not telegram_id:
        telegram_id = message.from_user.id
    logger.info("skin_anamnesis: ищем пользователя telegram_id=%s", telegram_id)

    user = await get_user_by_telegram_id(session, telegram_id)
    if not user:
        logger.warning("skin_anamnesis: пользователь не найден! telegram_id=%s", telegram_id)
        return

    user.skin_anamnesis_json = json.dumps(answers, ensure_ascii=False)
    from utils.helpers import now_salon
    user.skin_anamnesis_at = now_salon()
    await session.flush()
    logger.info("skin_anamnesis: анамнез сохранён для пользователя %s", user.telegram_id)

    # Компактный отчёт для Ашуры
    admin_msg = _build_admin_report(user, answers)

    try:
        await message.bot.send_message(chat_id=Config.ADMIN_ID, text=admin_msg, parse_mode="HTML")
        logger.info("skin_anamnesis: отчёт отправлен Ашуре (ID: %s)", Config.ADMIN_ID)
    except Exception as e:
        logger.error("skin_anamnesis: не удалось отправить отчёт Ашуре: %s", e)

    # Отправляем фото клиентки админу (ОРИГИНАЛ, не сжатый)
    if answers.get("photo_file_id"):
        try:
            await message.bot.send_photo(
                chat_id=Config.ADMIN_ID,
                photo=answers["photo_file_id"],
                caption=f"📸 Фото от <b>{html_escape(user.name)}</b>",
                parse_mode="HTML",
            )
            logger.info("skin_anamnesis: фото отправлено Ашуре")
        except Exception as e:
            logger.error("skin_anamnesis: ошибка отправки фото Ашуре: %s", e)

    # Подробные рекомендации от ИИ — ВСЕГДА генерируем отдельно для админа (for_admin=True)
    # Не используем ai_recommendations — это клиентская версия (for_admin=False)
    try:
        admin_recs = await _generate_recommendations(answers, for_admin=True, user_id=telegram_id)
        if admin_recs:
            rec_msg = (
                f"📋 <b>ПЛАН ДЛЯ {html_escape(user.name)}:</b>\n\n"
                f"{html_escape(admin_recs)}"
            )
            for chunk in split_message(rec_msg):
                await message.bot.send_message(chat_id=Config.ADMIN_ID, text=chunk, parse_mode="HTML")
            logger.info("skin_anamnesis: рекомендации отправлены Ашуре")
        else:
            logger.warning("skin_anamnesis: рекомендации пустые (Grok не ответил)")
    except Exception as e:
        logger.error("skin_anamnesis: ошибка генерации/отправки рекомендаций: %s", e)

    # AI-анализ фото (escape)
    if answers.get("ai_skin_analysis"):
        try:
            ai_msg = (
                f"📸 <b>AI-анализ фото от {html_escape(user.name)}:</b>\n\n"
                f"{html_escape(answers['ai_skin_analysis'][:1000])}"
            )
            await message.bot.send_message(chat_id=Config.ADMIN_ID, text=ai_msg, parse_mode="HTML")
        except Exception as e:
            logger.error("skin_anamnesis: ошибка отправки AI-анализа: %s", e)

    # Пересылка видео/голосового админу (с file_id)
    if answers.get("has_video"):
        try:
            if answers.get("video_file_id"):
                await message.bot.send_video(
                    chat_id=Config.ADMIN_ID,
                    video=answers["video_file_id"],
                    caption=f"📹 Видео от <b>{html_escape(user.name)}</b> (анамнез кожи)",
                    parse_mode="HTML",
                )
            else:
                await message.bot.send_message(
                    chat_id=Config.ADMIN_ID,
                    text=f"📹 <b>{html_escape(user.name)}</b> приложила видео к анамнезу (файл не сохранён)",
                    parse_mode="HTML",
                )
            logger.info("skin_anamnesis: видео отправлено Ашуре")
        except Exception as e:
            logger.warning("skin_anamnesis: ошибка отправки видео: %s", e)

    if answers.get("has_voice"):
        try:
            if answers.get("voice_file_id"):
                await message.bot.send_voice(
                    chat_id=Config.ADMIN_ID,
                    voice=answers["voice_file_id"],
                    caption=f"🎤 Голосовое от <b>{html_escape(user.name)}</b> (анамнез кожи)",
                    parse_mode="HTML",
                )
            else:
                await message.bot.send_message(
                    chat_id=Config.ADMIN_ID,
                    text=f"🎤 <b>{html_escape(user.name)}</b> приложила голосовое к анамнезу (файл не сохранён)",
                    parse_mode="HTML",
                )
            logger.info("skin_anamnesis: голосовое отправлено Ашуре")
        except Exception as e:
            logger.warning("skin_anamnesis: ошибка отправки голосового: %s", e)

    await state.clear()
