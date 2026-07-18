"""
Форматирование текстов для мобильных экранов Telegram.
Кнопки — до ~64 символов; длинные тексты — в теле сообщения.
"""

from __future__ import annotations

# Лимит подписи inline-кнопки (Telegram обрезает длинные)
BUTTON_LABEL_MAX = 40


def truncate_button(text: str, max_len: int = BUTTON_LABEL_MAX) -> str:
    """Укорачивает текст для inline-кнопки без обрезки на телефоне."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def wrap_lines(text: str, width: int = 42) -> str:
    """Переносит длинные строки для удобного чтения на телефоне."""
    if not text:
        return ""
    result: list[str] = []
    for paragraph in text.split("\n"):
        if len(paragraph) <= width:
            result.append(paragraph)
            continue
        words = paragraph.split()
        line: list[str] = []
        length = 0
        for word in words:
            add = len(word) + (1 if line else 0)
            if length + add > width:
                result.append(" ".join(line))
                line = [word]
                length = len(word)
            else:
                line.append(word)
                length += add
        if line:
            result.append(" ".join(line))
    return "\n".join(result)


def split_message(text: str, max_len: int = 3500) -> list[str]:
    """Делит очень длинный текст на несколько сообщений."""
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > max_len and current:
            parts.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        parts.append("\n".join(current))
    return parts