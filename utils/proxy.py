"""
Настройка прокси для Telegram API.
Поддержка Happ / Reality: автоопределение порта и PROXY_SSL_VERIFY=false.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
from typing import TYPE_CHECKING

import certifi
from aiohttp import ClientSession, ClientTimeout
from aiohttp_socks import ProxyConnector

from aiogram.client.session.aiohttp import AiohttpSession

if TYPE_CHECKING:
    from config import Config

logger = logging.getLogger(__name__)

# Порты локальных прокси Happ / V2Ray / Clash (по приоритету)
DEFAULT_PROXY_PORTS: tuple[int, ...] = (
    10808,  # Happ — mixed/SOCKS
    10809,  # Happ — HTTP
    1080,
    7890,
    7891,
    2080,
    1087,
)

DEFAULT_PROXY_SCHEMES: tuple[str, ...] = ("socks5", "http")


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_ssl_verify() -> bool:
    """PROXY_SSL_VERIFY=false отключает проверку сертификатов (нужно для Reality/Happ)."""
    return _parse_bool(os.getenv("PROXY_SSL_VERIFY", "false"), default=False)


def build_ssl_context(verify: bool) -> ssl.SSLContext:
    """SSL-контекст. verify=False нужен для Reality-прокси (Happ)."""
    if verify:
        return ssl.create_default_context(cafile=certifi.where())
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class TelegramAiohttpSession(AiohttpSession):
    """AiohttpSession с управлением проверкой SSL-сертификатов."""

    def __init__(
        self,
        *,
        ssl_verify: bool = True,
        proxy: str | None = None,
        limit: int = 100,
        **kwargs,
    ) -> None:
        self._ssl_verify = ssl_verify
        super().__init__(proxy=proxy, limit=limit, **kwargs)
        self._apply_ssl_context()

    def _setup_proxy_connector(self, proxy) -> None:
        super()._setup_proxy_connector(proxy)
        self._apply_ssl_context()

    def _apply_ssl_context(self) -> None:
        self._connector_init["ssl"] = build_ssl_context(self._ssl_verify)


def _is_local_port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_ports(raw: str) -> tuple[int, ...]:
    ports: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ports.append(int(part))
    return tuple(ports) if ports else DEFAULT_PROXY_PORTS


def _parse_schemes(raw: str) -> tuple[str, ...]:
    schemes = tuple(s.strip() for s in raw.split(",") if s.strip())
    return schemes if schemes else DEFAULT_PROXY_SCHEMES


async def _probe_proxy(
    scheme: str,
    host: str,
    port: int,
    *,
    ssl_verify: bool,
    timeout: float,
) -> bool:
    """Проверяет, что через прокси открывается Telegram API."""
    proxy_url = f"{scheme}://{host}:{port}"
    connector = ProxyConnector.from_url(
        proxy_url,
        ssl=build_ssl_context(ssl_verify),
        rdns=True,
    )
    try:
        async with ClientSession(
            connector=connector,
            timeout=ClientTimeout(total=timeout),
        ) as session:
            async with session.get("https://api.telegram.org") as resp:
                return resp.status in {200, 302, 401, 404}
    except Exception:
        return False
    finally:
        await connector.close()


async def detect_proxy_url(
    host: str = "127.0.0.1",
    ports: tuple[int, ...] = DEFAULT_PROXY_PORTS,
    schemes: tuple[str, ...] = DEFAULT_PROXY_SCHEMES,
    *,
    ssl_verify: bool = False,
    timeout: float = 12.0,
) -> str | None:
    """
    Автоопределение рабочего локального прокси.
    Сначала проверяет открытые порты, затем тестирует доступ к Telegram.
    """
    candidates: list[tuple[str, int]] = []
    for port in ports:
        if _is_local_port_open(host, port):
            for scheme in schemes:
                candidates.append((scheme, port))

    if not candidates:
        logger.warning(
            "PROXY_AUTO: на %s не найдены открытые порты из списка %s",
            host,
            ", ".join(str(p) for p in ports),
        )
        return None

    logger.info(
        "PROXY_AUTO: проверяю %s вариант(ов) прокси на %s...",
        len(candidates),
        host,
    )

    for scheme, port in candidates:
        proxy_url = f"{scheme}://{host}:{port}"
        logger.info("PROXY_AUTO: тест %s ...", proxy_url)
        if await _probe_proxy(
            scheme,
            host,
            port,
            ssl_verify=ssl_verify,
            timeout=timeout,
        ):
            logger.info("PROXY_AUTO: рабочий прокси найден — %s", proxy_url)
            return proxy_url
        logger.debug("PROXY_AUTO: %s не подошёл", proxy_url)

    logger.warning("PROXY_AUTO: ни один локальный прокси не прошёл проверку Telegram")
    return None


async def resolve_proxy_url(config: type[Config]) -> tuple[str | None, bool]:
    """Полное разрешение прокси, включая автоопределение порта."""
    proxy_url = (config.PROXY_URL or "").strip()
    ssl_verify = get_ssl_verify()

    if proxy_url:
        return proxy_url, ssl_verify

    if config.PROXY_AUTO:
        detected = await detect_proxy_url(
            host=config.PROXY_HOST,
            ports=config.PROXY_PORTS,
            schemes=config.PROXY_SCHEMES,
            ssl_verify=ssl_verify,
            timeout=float(config.PROXY_PROBE_TIMEOUT),
        )
        return detected, ssl_verify

    return None, ssl_verify


def create_telegram_session(
    config: type[Config],
    proxy_url: str | None,
    *,
    ssl_verify: bool,
) -> TelegramAiohttpSession:
    """Создаёт HTTP-сессию aiogram с прокси и нужным SSL-контекстом."""
    session = TelegramAiohttpSession(
        proxy=proxy_url,
        ssl_verify=ssl_verify,
        timeout=config.TELEGRAM_TIMEOUT,
    )
    if proxy_url:
        from urllib.parse import urlparse

        parsed = urlparse(proxy_url)
        ssl_mode = "проверка включена" if ssl_verify else "проверка ОТКЛЮЧЕНА (Reality/Happ)"
        logger.info(
            "Прокси: %s://%s:%s | SSL: %s",
            parsed.scheme,
            parsed.hostname,
            parsed.port,
            ssl_mode,
        )
    else:
        logger.warning(
            "Прокси не задан. Если Telegram заблокирован — включите PROXY_AUTO=true "
            "или укажите PROXY_URL в .env"
        )
    return session


async def verify_telegram_via_proxy(
    bot_token: str,
    proxy_url: str | None,
    *,
    ssl_verify: bool,
    timeout: float = 30.0,
) -> bool:
    """Проверяет getMe через выбранный прокси перед запуском polling."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    session = TelegramAiohttpSession(
        proxy=proxy_url,
        ssl_verify=ssl_verify,
        timeout=int(timeout),
    )
    bot = Bot(
        token=bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        me = await bot.get_me()
        logger.info("Telegram API OK: @%s (%s)", me.username, me.first_name)
        return True
    finally:
        await bot.session.close()