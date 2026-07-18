"""
Клиент Grok API (xAI) для ИИ-консультанта.
Использует тот же прокси, что и Telegram-бот.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout
from aiohttp_socks import ProxyConnector

from config import Config
from utils.proxy import build_ssl_context, get_ssl_verify, resolve_proxy_url

logger = logging.getLogger(__name__)

# Daily API request counter
_daily_requests: int = 0
_daily_date: date | None = None

# Per-user daily tracking
_user_daily: dict[int, int] = {}
_user_daily_date: date | None = None

# Concurrency semaphore for AI requests
_ai_sem = asyncio.Semaphore(Config.AI_MAX_CONCURRENT)

PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "ai_consultant.txt"
)
SKIN_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "skin_analysis.txt"
)

VISION_MODEL = Config.XAI_VISION_MODEL

# Переиспользуемая HTTP-сессия для Grok API
_grok_session: ClientSession | None = None
_grok_session_proxy: str | None = None
_resolved_proxy_url: str | None = None
_proxy_resolved: bool = False
_session_created_at: float = 0
_SESSION_TTL_SECONDS = 3600  # Пересоздаём сессию каждый час


class GrokAPIError(Exception):
    """Ошибка при обращении к Grok API."""


def _check_daily_limit() -> None:
    """Проверяет дневной лимит запросов к Grok API. Сбрасывается каждый день."""
    global _daily_requests, _daily_date
    today = date.today()
    if _daily_date != today:
        _daily_requests = 0
        _daily_date = today
    if Config.AI_DAILY_LIMIT > 0 and _daily_requests >= Config.AI_DAILY_LIMIT:
        raise GrokAPIError(
            f"Достигнут дневной лимит запросов к ИИ ({Config.AI_DAILY_LIMIT}). Попробуйте завтра."
        )
    _daily_requests += 1


def _check_user_limit(user_id: int) -> None:
    """Проверяет per-user дневной лимит. Сбрасывается каждый день."""
    global _user_daily, _user_daily_date
    today = date.today()
    if _user_daily_date != today:
        _user_daily.clear()
        _user_daily_date = today
    count = _user_daily.get(user_id, 0)
    if Config.AI_DAILY_LIMIT_PER_USER > 0 and count >= Config.AI_DAILY_LIMIT_PER_USER:
        raise GrokAPIError(
            f"Вы превысили дневной лимит запросов к ИИ ({Config.AI_DAILY_LIMIT_PER_USER}). Попробуйте завтра."
        )
    _user_daily[user_id] = count + 1


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """Загружает системный промпт ИИ-консультанта."""
    if PROMPT_PATH.is_file():
        return PROMPT_PATH.read_text(encoding="utf-8").strip()
    logger.warning("Файл промпта не найден: %s", PROMPT_PATH)
    return (
        "Ты — виртуальный помощник косметолога Ашуры. "
        "Отвечай кратко на русском, без диагнозов и точных цен."
    )


@lru_cache(maxsize=1)
def load_skin_prompt() -> str:
    """Загружает системный промпт для анализа кожи."""
    if SKIN_PROMPT_PATH.is_file():
        return SKIN_PROMPT_PATH.read_text(encoding="utf-8").strip()
    logger.warning("Файл промпта не найден: %s", SKIN_PROMPT_PATH)
    return (
        "Ты — ИИ-консультант косметолога Ашуры. "
        "Анализируй фото кожи и давай общие рекомендации. "
        "Не ставь диагнозов. Рекомендуй запись к Ашуре."
    )


def _sanitize_user_message(text: str) -> str:
    """Basic prompt injection defense — strips common injection patterns."""
    injections = [
        'ignore previous instructions',
        'ignore all previous',
        'you are now',
        'new instructions:',
        'system prompt:',
        'forget everything',
        'disregard',
    ]
    lower = text.lower()
    for injection in injections:
        if injection in lower:
            return '[Сообщение отфильтровано]'
    return text


def trim_history(history: list[dict], limit: int) -> list[dict]:
    """Оставляет последние N сообщений диалога (без system)."""
    if limit <= 0 or len(history) <= limit:
        return history
    return history[-limit:]


async def _get_http_session(proxy_url: str | None) -> ClientSession:
    """Возвращает переиспользуемую HTTP-сессию (создаёт при необходимости)."""
    import time
    global _grok_session, _grok_session_proxy, _session_created_at

    now = time.monotonic()
    needs_new = (
        _grok_session is None
        or _grok_session.closed
        or _grok_session_proxy != proxy_url
        or (now - _session_created_at) > _SESSION_TTL_SECONDS
    )

    if not needs_new:
        return _grok_session

    if _grok_session is not None and not _grok_session.closed:
        await _grok_session.close()

    from aiohttp import TCPConnector

    ssl_verify = get_ssl_verify()
    ssl_ctx = build_ssl_context(ssl_verify)

    if proxy_url:
        connector = ProxyConnector.from_url(
            proxy_url,
            ssl=ssl_ctx,
            rdns=True,
        )
    else:
        connector = TCPConnector(ssl=ssl_ctx)

    _grok_session = ClientSession(
        connector=connector,
        timeout=ClientTimeout(total=Config.XAI_TIMEOUT),
        connector_owner=True,
    )
    # Ограничиваем количество одновременных соединений
    if hasattr(connector, '_limit'):
        connector._limit = 20
        connector._limit_per_host = 10
    _grok_session_proxy = proxy_url
    _session_created_at = now
    return _grok_session


async def _get_cached_proxy_url() -> str | None:
    """Кеширует результат resolve_proxy_url — один раз за жизнь процесса."""
    global _resolved_proxy_url, _proxy_resolved
    if not _proxy_resolved:
        _resolved_proxy_url, _ = await resolve_proxy_url(Config)
        _proxy_resolved = True
    return _resolved_proxy_url


async def ask_grok(
    history: list[dict],
    model: str = "",
    system_prompt: str = "",
    user_id: int = 0,
) -> str:
    """
    Отправляет историю диалога в Grok API и возвращает ответ ассистента.
    history — список {"role": "user"|"assistant", "content": "..."}.
    model — если не указан, используется Config.XAI_MODEL.
    system_prompt — если не указан, загружается из файла.
    user_id — если указан, проверяется per-user дневной лимит.
    """
    if not Config.XAI_API_KEY:
        raise GrokAPIError("XAI_API_KEY не задан в .env")

    _check_daily_limit()
    if user_id:
        _check_user_limit(user_id)

    async with _ai_sem:
        proxy_url = await _get_cached_proxy_url()
        messages = [{"role": "system", "content": system_prompt or load_system_prompt()}]
        for msg in trim_history(history, Config.AI_HISTORY_LIMIT):
            if msg.get('role') == 'user' and isinstance(msg.get('content'), str):
                messages.append({**msg, 'content': _sanitize_user_message(msg['content'])})
            else:
                messages.append(msg)

        url = f"{Config.XAI_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {Config.XAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or Config.XAI_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1024,
        }

        session = await _get_http_session(proxy_url)
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    detail = body.get("error", body) if isinstance(body, dict) else body
                    logger.error("Grok API %s: %s", resp.status, detail)
                    raise GrokAPIError(f"Grok API вернул код {resp.status}")

                choices = body.get("choices") or []
                if not choices:
                    raise GrokAPIError("Grok API вернул пустой ответ")

                message = choices[0].get("message") or {}
                content = (message.get("content") or "").strip()
                if not content:
                    raise GrokAPIError("Grok API вернул пустой текст")
                return content
        except GrokAPIError:
            raise
        except Exception as e:
            logger.exception("Ошибка запроса к Grok API: %s", e)
            raise GrokAPIError("Не удалось связаться с ИИ-консультантом") from e


async def ask_grok_vision(
    image_bytes: bytes,
    user_text: str = "",
    mime_type: str = "image/jpeg",
    user_id: int = 0,
) -> str:
    """
    Отправляет изображение в Grok Vision API для анализа кожи.
    image_bytes — сырые байты изображения.
    user_text — опциональный текстовый запрос пользователя.
    mime_type — MIME-тип изображения.
    user_id — если указан, проверяется per-user дневной лимит.
    """
    if not Config.XAI_API_KEY:
        raise GrokAPIError("XAI_API_KEY не задан в .env")

    _check_daily_limit()
    if user_id:
        _check_user_limit(user_id)

    async with _ai_sem:
        proxy_url = await _get_cached_proxy_url()

        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64_image}"

        user_content: list[dict] = []
        user_content.append({
            "type": "image_url",
            "image_url": {"url": data_url, "detail": "high"},
        })
        if user_text:
            user_content.append({"type": "text", "text": _sanitize_user_message(user_text)})
        else:
            user_content.append({
                "type": "text",
                "text": "Проанализируй состояние кожи на этом фото.",
            })

        messages = [
            {"role": "system", "content": load_skin_prompt()},
            {"role": "user", "content": user_content},
        ]

        url = f"{Config.XAI_BASE_URL.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {Config.XAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": VISION_MODEL,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 1024,
        }

        session = await _get_http_session(proxy_url)
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    detail = body.get("error", body) if isinstance(body, dict) else body
                    logger.error("Grok Vision API %s: %s", resp.status, detail)
                    raise GrokAPIError(f"Grok Vision API вернул код {resp.status}")

                choices = body.get("choices") or []
                if not choices:
                    raise GrokAPIError("Grok Vision API вернул пустой ответ")

                message = choices[0].get("message") or {}
                content = (message.get("content") or "").strip()
                if not content:
                    raise GrokAPIError("Grok Vision API вернул пустой текст")
                return content
        except GrokAPIError:
            raise
        except Exception as e:
            logger.exception("Ошибка запроса к Grok Vision API: %s", e)
            raise GrokAPIError("Не удалось проанализировать изображение") from e