"""
Swarm Mode — параллельный запрос к Grok + Kimi K3 для получения лучшего ответа.
Использование:
    from utils.swarm import ask_swarm
    result = await ask_swarm(history, system_prompt="...")
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def ask_swarm(
    history: list[dict],
    system_prompt: str = "",
    grok_model: str = "grok-4.5",
    kimi_model: str = "kimi-k3",
    max_tokens: int = 2048,
) -> str:
    """
    Swarm mode: отправляет запрос параллельно в Grok и Kimi K3,
    возвращает лучший ответ (или оба если оба хороши).
    """
    from utils.grok import ask_grok, GrokAPIError
    from utils.kimi import ask_kimi, KimiAPIError

    async def _ask_grok():
        try:
            return await ask_grok(history, system_prompt=system_prompt, model=grok_model)
        except GrokAPIError as e:
            logger.warning("Swarm Grok failed: %s", e)
            return None

    async def _ask_kimi():
        try:
            return await ask_kimi(history, system_prompt=system_prompt, model=kimi_model, max_tokens=max_tokens)
        except KimiAPIError as e:
            logger.warning("Swarm Kimi failed: %s", e)
            return None

    # Параллельный запрос
    grok_result, kimi_result = await asyncio.gather(_ask_grok(), _ask_kimi())

    # Логика выбора лучшего ответа
    if grok_result and kimi_result:
        # Оба ответили — выбираем более длинный (обычно более подробный)
        if len(kimi_result) > len(grok_result) * 1.5:
            logger.info("Swarm: Kimi wins (%d vs %d chars)", len(kimi_result), len(grok_result))
            return kimi_result
        else:
            logger.info("Swarm: Grok wins (%d vs %d chars)", len(grok_result), len(kimi_result))
            return grok_result
    elif grok_result:
        logger.info("Swarm: only Grok answered")
        return grok_result
    elif kimi_result:
        logger.info("Swarm: only Kimi answered")
        return kimi_result
    else:
        raise Exception("Swarm: оба модели не ответили (Grok + Kimi K3)")


async def ask_swarm_parallel(
    history: list[dict],
    system_prompt: str = "",
) -> dict:
    """
    Возвращает ответы обеих моделей отдельно для сравнения.
    """
    from utils.grok import ask_grok, GrokAPIError
    from utils.kimi import ask_kimi, KimiAPIError

    async def _ask_grok():
        try:
            return await ask_grok(history, system_prompt=system_prompt)
        except GrokAPIError as e:
            return f"ERROR: {e}"

    async def _ask_kimi():
        try:
            return await ask_kimi(history, system_prompt=system_prompt)
        except KimiAPIError as e:
            return f"ERROR: {e}"

    grok_result, kimi_result = await asyncio.gather(_ask_grok(), _ask_kimi())

    return {
        "grok": grok_result,
        "kimi": kimi_result,
    }
