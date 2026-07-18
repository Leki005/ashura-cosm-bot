"""
Согласие на обработку персональных данных (152-ФЗ).
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from database import PersonalDataConsentLog, User
from utils.text_format import split_message

POLICY_PATH = Path(__file__).resolve().parent.parent / "prompts" / "privacy_policy.txt"

CONSENT_INTRO = (
    "📄 <b>Согласие на обработку персональных данных</b>\n\n"
    "Для работы с ботом необходимо ваше согласие на обработку персональных данных "
    "в соответствии с законодательством РФ (152-ФЗ).\n\n"
    "Мы обрабатываем: имя, телефон, Telegram ID, данные анкеты и записей — "
    "только для записи на процедуры, консультаций и бонусной программы.\n\n"
    "Ознакомьтесь с полным текстом согласия или подтвердите его кнопкой ниже.\n"
    "<i>Согласие запрашивается один раз.</i>"
)

CONSENT_DECLINED = (
    "Без согласия на обработку персональных данных мы не можем "
    "принимать и хранить ваши данные.\n\n"
    "Если передумаете — нажмите /start и подтвердите согласие."
)


@lru_cache(maxsize=1)
def load_privacy_policy() -> str:
    """Полный текст политики / согласия."""
    if POLICY_PATH.is_file():
        return POLICY_PATH.read_text(encoding="utf-8").strip()
    return (
        "Согласие на обработку персональных данных. "
        f"Оператор: {Config.SALON_NAME}, {Config.SALON_ADDRESS}."
    )


def has_pd_consent(user: User | None) -> bool:
    """Проверяет, давал ли пользователь согласие на обработку ПДн."""
    if user is None or user.pd_consent_at is None:
        return False
    # Проверяем версию согласия — если изменилась, нужно дать заново
    if user.pd_consent_version and user.pd_consent_version != Config.PRIVACY_POLICY_VERSION:
        return False
    return True


def format_pd_consent_admin_line(user: User) -> str:
    """Строка о согласии на ПДн для уведомлений админу."""
    if has_pd_consent(user):
        when = user.pd_consent_at.strftime("%d.%m.%Y %H:%M")
        ver = user.pd_consent_version or Config.PRIVACY_POLICY_VERSION
        return f"📄 ПДн (152-ФЗ): ✅ согласие получено ({when}, v{ver})\n"
    return "📄 ПДн (152-ФЗ): ❌ согласие не получено\n"


async def send_policy_text(message) -> None:
    """Отправляет полный текст согласия (может быть несколькими сообщениями)."""
    chunks = split_message(load_privacy_policy(), max_len=3500)
    for chunk in chunks:
        await message.answer(chunk)


async def log_pd_consent(
    session: AsyncSession,
    user: User,
    *,
    consented_at: datetime | None = None,
) -> None:
    """Сохраняет согласие в профиле пользователя и в журнале."""
    from utils.helpers import now_salon
    now = consented_at or now_salon()
    user.pd_consent_at = now
    user.pd_consent_version = Config.PRIVACY_POLICY_VERSION

    session.add(
        PersonalDataConsentLog(
            user_id=user.id,
            telegram_id=user.telegram_id,
            policy_version=Config.PRIVACY_POLICY_VERSION,
            consented_at=now,
            name=user.name,
            phone=user.phone,
        )
    )
    await session.flush()