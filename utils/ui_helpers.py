"""
UI helpers для красивых анимаций и визуализаций в Telegram-боте AshuraCosm.
Прогресс-бары, форматирование, callback feedback.
"""

from aiogram.types import CallbackQuery


def progress_bar(q: int, total: int = 11) -> str:
    """
    Unicode-прогресс-бар для анамнеза кожи.

    Примеры:
        Шаг 1  →  ▓░░░░░░░░░ 0%
        Шаг 5  →  ▓▓▓▓░░░░░░ 36%
        Шаг 11 →  ▓▓▓▓▓▓▓▓▓▓ 100%
    """
    if q >= total:
        pct, filled = 100, 10
    else:
        pct = round((q - 1) / total * 100)
        filled = round((q - 1) / total * 10)
    bar = "▓" * filled + "░" * (10 - filled)

    if q == 1:
        icon = "✨"
    elif q == total:
        icon = "🏁"
    else:
        icon = "📋"

    return f"{icon} <b>Вопрос {q} из {total}</b>\n<code>{bar}</code> {pct}%"


async def cb_ack(callback: CallbackQuery, action: str = "select") -> None:
    """
    Мгновенный callback feedback с эмодзи.
    Показывает всплывающую подсказку на 1-2 сек.

    action: select, remove, next, back, error, done, loading
    """
    messages = {
        "select": "✅ Выбрано",
        "remove": "↩️ Убрано",
        "next": "➡️ Далее",
        "back": "⬅️ Назад",
        "error": "⚠️ Выберите вариант",
        "done": "✨ Готово",
        "loading": "⏳ Загружаю...",
    }
    text = messages.get(action, "✅ Готово")
    await callback.answer(text)
