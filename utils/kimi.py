"""
Kimi API Wrapper v2 -- optimized with streaming and retry.
Endpoint: https://api.moonshot.ai/v1
Models: kimi-k3 (thinking), kimi-k2.7-code-highspeed (180 tok/s), kimi-k2.6
"""
import asyncio
import json
import logging
import time
from typing import AsyncIterator

import aiohttp
from aiohttp_socks import ProxyConnector
from config import Config

logger = logging.getLogger(__name__)

KIMI_API_KEY = Config.KIMI_API_KEY
KIMI_BASE_URL = Config.KIMI_BASE_URL
KIMI_MODEL = Config.KIMI_MODEL
KIMI_PROXY = Config.KIMI_PROXY
KIMI_TIMEOUT = max(Config.KIMI_TIMEOUT, 300)

_kimi_session = None
_kimi_session_created = 0
_SESSION_TTL = 3600


class KimiAPIError(Exception):
    pass


async def _get_session():
    global _kimi_session, _kimi_session_created
    now = time.monotonic()
    if _kimi_session is None or _kimi_session.closed or (now - _kimi_session_created > _SESSION_TTL):
        if _kimi_session and not _kimi_session.closed:
            await _kimi_session.close()
        connector = ProxyConnector.from_url(KIMI_PROXY, rdns=True)
        _kimi_session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=KIMI_TIMEOUT, connect=30, sock_connect=30, sock_read=KIMI_TIMEOUT - 60),
        )
        _kimi_session_created = now
        logger.info("Kimi session created timeout=%ds", KIMI_TIMEOUT)
    return _kimi_session


def _build_payload(messages, model, max_tokens, temperature, stream=False, thinking=None):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if stream:
        payload["stream"] = True
    if thinking is not None:
        payload["thinking"] = thinking
    return payload


async def ask_kimi(history, system_prompt="", model=None, max_tokens=2048, temperature=1.0, retries=2, thinking=None):
    if not KIMI_API_KEY:
        raise KimiAPIError("KIMI_API_KEY not set")
    model = model or KIMI_MODEL
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    headers = {"Authorization": "Bearer " + KIMI_API_KEY, "Content-Type": "application/json"}
    payload = _build_payload(messages, model, max_tokens, temperature, stream=False)
    last_error = None
    for attempt in range(retries + 1):
        try:
            session = await _get_session()
            start = time.monotonic()
            async with session.post(KIMI_BASE_URL + "/chat/completions", json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                elapsed = time.monotonic() - start
                if resp.status != 200:
                    detail = body.get("error", body) if isinstance(body, dict) else body
                    raise KimiAPIError("Kimi " + str(resp.status) + ": " + str(detail))
                choices = body.get("choices") or []
                if not choices:
                    raise KimiAPIError("Empty response")
                msg = choices[0].get("message") or {}
                content = (msg.get("content") or "").strip()
                reasoning = (msg.get("reasoning_content") or "").strip()
                usage = body.get("usage") or {}
                p = usage.get("prompt_tokens", 0)
                c = usage.get("completion_tokens", 0)
                r = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
                logger.info("Kimi %s: %.1fs | %din + %dout (%dreasoning)", model, elapsed, p, c, r)
                if not content and reasoning:
                    logger.warning("Kimi %s: content empty, reasoning=%d tokens", model, r)
                    return reasoning  # Возвращаем reasoning целиком, не обрезаем
                if not content:
                    raise KimiAPIError("Empty text")
                return content
        except KimiAPIError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last_error = e
            if attempt < retries:
                wait = (attempt + 1) * 3
                logger.warning("Kimi %s attempt %d: %s. Retry %ds...", model, attempt + 1, type(e).__name__, wait)
                global _kimi_session
                if _kimi_session and not _kimi_session.closed:
                    await _kimi_session.close()
                _kimi_session = None
                await asyncio.sleep(wait)
        except Exception as e:
            logger.exception("Kimi %s unexpected: %s", model, e)
            raise KimiAPIError(str(e)) from e
    raise KimiAPIError("Timeout after " + str(retries + 1) + " attempts: " + str(last_error))


async def ask_kimi_stream(history, system_prompt="", model=None, max_tokens=4096, temperature=1.0):
    if not KIMI_API_KEY:
        raise KimiAPIError("KIMI_API_KEY not set")
    model = model or KIMI_MODEL
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    headers = {"Authorization": "Bearer " + KIMI_API_KEY, "Content-Type": "application/json"}
    payload = _build_payload(messages, model, max_tokens, temperature, stream=True)
    session = await _get_session()
    async with session.post(KIMI_BASE_URL + "/chat/completions", json=payload, headers=headers) as resp:
        if resp.status != 200:
            body = await resp.json(content_type=None)
            detail = body.get("error", body) if isinstance(body, dict) else body
            raise KimiAPIError("Kimi " + str(resp.status) + ": " + str(detail))
        buffer = ""
        async for chunk in resp.content.iter_any():
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        pass


async def ask_kimi_fast(history, system_prompt="", max_tokens=2048):
    return await ask_kimi(
        history=history,
        system_prompt=system_prompt,
        model="kimi-k2.7-code-highspeed",
        max_tokens=max_tokens,
        temperature=1.0,
        retries=1,
    )


async def close_kimi_session():
    global _kimi_session, _kimi_session_created
    if _kimi_session and not _kimi_session.closed:
        await _kimi_session.close()
        logger.info("Kimi session closed")
    _kimi_session = None
    _kimi_session_created = 0
