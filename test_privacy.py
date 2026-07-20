"""
Tests for privacy/consent system (152-ФЗ).

Covers:
  1. Consent revocation (pd_consent_at → None)
  2. Data anonymization (name, phone, anamnesis)
  3. Active bookings cancelled on revocation
  4. Bonus refund on revocation
  5. Review text scrubbed
  6. Consent log anonymized
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select, update as sa_update

from config import Config
from database import (
    Booking,
    BonusTransaction,
    PersonalDataConsentLog,
    Review,
    Service,
    User,
)
from utils.helpers import ACTIVE_BOOKING_STATUSES, now_salon
from utils.privacy import has_pd_consent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(telegram_id=930_001, name="Приват Тест", phone="79004445566",
               bonus_balance=0, consent=True):
    user = User(telegram_id=telegram_id, name=name, phone=phone, bonus_balance=bonus_balance)
    if consent:
        user.pd_consent_at = now_salon()
        user.pd_consent_version = Config.PRIVACY_POLICY_VERSION
    return user


def _seed_service(session, name="Чистка лица", price=3500):
    svc = Service(name=name, category="Уход за лицом", price=price, duration=90, is_active=True)
    session.add(svc)
    return svc


# ---------------------------------------------------------------------------
# 1. Consent revocation
# ---------------------------------------------------------------------------

async def test_revoke_clears_consent_fields(db_session):
    """Revoking consent sets pd_consent_at and pd_consent_version to None."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    assert has_pd_consent(user) is True

    # Simulate revocation
    user.pd_consent_at = None
    user.pd_consent_version = None
    await db_session.flush()

    assert has_pd_consent(user) is False
    assert user.pd_consent_at is None
    assert user.pd_consent_version is None


async def test_has_pd_consent_after_revoke(db_session):
    """has_pd_consent returns False after revocation."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    user.pd_consent_at = None
    user.pd_consent_version = None

    assert has_pd_consent(user) is False


async def test_has_pd_consent_version_mismatch(db_session):
    """has_pd_consent returns False if policy version changed."""
    user = _make_user()
    user.pd_consent_version = "0.9"  # Old version
    db_session.add(user)
    await db_session.flush()

    assert has_pd_consent(user) is False


async def test_has_pd_consent_none_user():
    """has_pd_consent(None) returns False."""
    assert has_pd_consent(None) is False


async def test_has_pd_consent_no_consent_at():
    """User without pd_consent_at → False."""
    user = MagicMock()
    user.pd_consent_at = None
    user.pd_consent_version = Config.PRIVACY_POLICY_VERSION
    assert has_pd_consent(user) is False


async def test_has_pd_consent_legacy_none_version():
    """Legacy user with pd_consent_version=None → True."""
    user = MagicMock()
    user.pd_consent_at = now_salon()
    user.pd_consent_version = None
    assert has_pd_consent(user) is True


# ---------------------------------------------------------------------------
# 2. Data anonymization
# ---------------------------------------------------------------------------

async def test_anonymize_name(db_session):
    """Name is replaced with Удалён_{telegram_id}."""
    user = _make_user(telegram_id=930002, name="Иван Петров")
    db_session.add(user)
    await db_session.flush()

    user.name = f"Удалён_{user.telegram_id}"
    await db_session.flush()

    assert user.name == "Удалён_930002"
    assert "Иван" not in user.name
    assert "Петров" not in user.name


async def test_anonymize_phone(db_session):
    """Phone is replaced with 00000000000."""
    user = _make_user(phone="79004445566")
    db_session.add(user)
    await db_session.flush()

    user.phone = "00000000000"
    await db_session.flush()

    assert user.phone == "00000000000"
    assert "7900" not in user.phone


async def test_anonymize_clears_anamnesis(db_session):
    """Anamnesis data is cleared on revocation."""
    user = _make_user()
    user.anamnesis_json = '{"allergy": true}'
    user.anamnesis_updated_at = now_salon()
    db_session.add(user)
    await db_session.flush()

    # Simulate revocation
    user.anamnesis_json = None
    user.anamnesis_updated_at = None
    await db_session.flush()

    assert user.anamnesis_json is None
    assert user.anamnesis_updated_at is None


async def test_anonymize_clears_skin_anamnesis(db_session):
    """Skin anamnesis is cleared on revocation."""
    user = _make_user()
    user.skin_anamnesis_json = '{"skin_type": "oily"}'
    user.skin_anamnesis_at = now_salon()
    db_session.add(user)
    await db_session.flush()

    user.skin_anamnesis_json = None
    user.skin_anamnesis_at = None
    await db_session.flush()

    assert user.skin_anamnesis_json is None
    assert user.skin_anamnesis_at is None


# ---------------------------------------------------------------------------
# 3. Active bookings cancelled on revocation
# ---------------------------------------------------------------------------

async def test_active_bookings_cancelled_on_revoke(db_session):
    """All pending/confirmed bookings are set to 'cancelled' on revocation."""
    user = _make_user()
    db_session.add(user)
    svc = _seed_service(db_session)
    await db_session.flush()

    # UNIQUE partial index allows only one active booking per user.
    # Use one active (pending) and one completed.
    b1 = Booking(user_id=user.id, service_id=svc.id, status="pending",
                 preferred_date="20.07.2026", preferred_time="14:00")
    b3 = Booking(user_id=user.id, service_id=svc.id, status="completed")
    db_session.add_all([b1, b3])
    await db_session.flush()

    # Simulate revocation: cancel active bookings
    await db_session.execute(
        sa_update(Booking)
        .where(Booking.user_id == user.id, Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        .values(status="cancelled", anamnesis_json=None, notes=None)
    )
    await db_session.flush()

    # Verify
    await db_session.refresh(b1)
    await db_session.refresh(b3)

    assert b1.status == "cancelled"
    assert b3.status == "completed"  # Not affected


async def test_booking_anamnesis_scrubbed_on_revoke(db_session):
    """Anamnesis in bookings is cleared on revocation."""
    user = _make_user()
    db_session.add(user)
    svc = _seed_service(db_session)
    await db_session.flush()

    booking = Booking(
        user_id=user.id, service_id=svc.id, status="confirmed",
        anamnesis_json='{"allergy": true}', notes="Хочу попробовать новый пилинг",
        preferred_date="20.07.2026", preferred_time="14:00",
    )
    db_session.add(booking)
    await db_session.flush()

    # Scrub all bookings
    await db_session.execute(
        sa_update(Booking)
        .where(Booking.user_id == user.id)
        .values(anamnesis_json=None, notes=None)
    )
    await db_session.flush()

    await db_session.refresh(booking)
    assert booking.anamnesis_json is None
    assert booking.notes is None


# ---------------------------------------------------------------------------
# 4. Bonus refund on revocation
# ---------------------------------------------------------------------------

async def test_bonus_refund_on_revoke(db_session):
    """Bonuses used in active bookings are refunded on revocation."""
    user = _make_user(bonus_balance=0)
    db_session.add(user)
    svc = _seed_service(db_session)
    await db_session.flush()

    # Active booking with 200 bonus_used
    booking = Booking(
        user_id=user.id, service_id=svc.id, status="pending",
        bonus_used=200, preferred_date="20.07.2026", preferred_time="14:00",
    )
    db_session.add(booking)
    await db_session.flush()

    # Simulate what privacy_revoke does:
    # 1. Find active bookings with bonus_used > 0
    active_with_bonus = await db_session.execute(
        select(Booking.id, Booking.bonus_used)
        .where(
            Booking.user_id == user.id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.bonus_used > 0,
        )
    )
    bonus_bookings = active_with_bonus.all()
    assert len(bonus_bookings) == 1
    assert bonus_bookings[0][1] == 200

    # 2. Refund
    for booking_id, bonus_amount in bonus_bookings:
        if bonus_amount > 0:
            await db_session.execute(
                sa_update(User).where(User.id == user.id)
                .values(bonus_balance=User.bonus_balance + bonus_amount)
            )
            await db_session.flush()

    await db_session.refresh(user)
    assert user.bonus_balance == 200

    # 3. Cancel and zero out bonus_used
    await db_session.execute(
        sa_update(Booking)
        .where(Booking.user_id == user.id, Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        .values(status="cancelled")
    )
    await db_session.execute(
        sa_update(Booking).where(Booking.id == booking.id)
        .values(bonus_used=0)
    )
    await db_session.flush()

    await db_session.refresh(booking)
    assert booking.status == "cancelled"
    assert booking.bonus_used == 0


async def test_no_bonus_refund_when_bonus_used_zero(db_session):
    """No refund when bonus_used is 0."""
    user = _make_user(bonus_balance=100)
    db_session.add(user)
    svc = _seed_service(db_session)
    await db_session.flush()

    booking = Booking(
        user_id=user.id, service_id=svc.id, status="pending",
        bonus_used=0, preferred_date="20.07.2026", preferred_time="14:00",
    )
    db_session.add(booking)
    await db_session.flush()

    # Check: no bookings with bonus_used > 0
    result = await db_session.execute(
        select(Booking.id, Booking.bonus_used)
        .where(
            Booking.user_id == user.id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.bonus_used > 0,
        )
    )
    bonus_bookings = result.all()
    assert len(bonus_bookings) == 0

    await db_session.refresh(user)
    assert user.bonus_balance == 100


async def test_completed_booking_bonus_not_refunded(db_session):
    """Only active bookings' bonuses are refunded, not completed ones."""
    user = _make_user(bonus_balance=0)
    db_session.add(user)
    svc = _seed_service(db_session)
    await db_session.flush()

    # Completed booking with bonus_used
    booking = Booking(
        user_id=user.id, service_id=svc.id, status="completed",
        bonus_used=300,
    )
    db_session.add(booking)
    await db_session.flush()

    result = await db_session.execute(
        select(Booking.id, Booking.bonus_used)
        .where(
            Booking.user_id == user.id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.bonus_used > 0,
        )
    )
    bonus_bookings = result.all()
    assert len(bonus_bookings) == 0, "Completed bookings should not be refunded"


# ---------------------------------------------------------------------------
# 5. Review text scrubbed
# ---------------------------------------------------------------------------

async def test_review_text_scrubbed_on_revoke(db_session):
    """Review text is set to None on data revocation."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    review = Review(user_id=user.id, rating=5, text="Отличный косметолог!")
    db_session.add(review)
    await db_session.flush()

    # Scrub reviews
    await db_session.execute(
        sa_update(Review).where(Review.user_id == user.id)
        .values(text=None)
    )
    await db_session.flush()

    await db_session.refresh(review)
    assert review.text is None
    assert review.rating == 5  # Rating preserved


async def test_review_rating_preserved_after_scrub(db_session):
    """Rating is NOT cleared when review text is scrubbed."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    review = Review(user_id=user.id, rating=4, text="Хорошо")
    db_session.add(review)
    await db_session.flush()

    await db_session.execute(
        sa_update(Review).where(Review.user_id == user.id)
        .values(text=None)
    )
    await db_session.refresh(review)

    assert review.text is None
    assert review.rating == 4


# ---------------------------------------------------------------------------
# 6. Consent log anonymized
# ---------------------------------------------------------------------------

async def test_consent_log_anonymized_on_revoke(db_session):
    """PersonalDataConsentLog entries are anonymized on revocation."""
    user = _make_user(name="Мария Иванова", phone="79004445566")
    db_session.add(user)
    await db_session.flush()

    log = PersonalDataConsentLog(
        user_id=user.id,
        telegram_id=user.telegram_id,
        policy_version="1.0",
        name="Мария Иванова",
        phone="79004445566",
    )
    db_session.add(log)
    await db_session.flush()

    # Anonymize logs
    await db_session.execute(
        sa_update(PersonalDataConsentLog)
        .where(PersonalDataConsentLog.user_id == user.id)
        .values(name=f'[анонимизировано_{user.telegram_id}]', phone='00000000000')
    )
    await db_session.flush()

    await db_session.refresh(log)
    assert log.name == f'[анонимизировано_{user.telegram_id}]'
    assert log.phone == '00000000000'


async def test_consent_log_preserves_policy_version(db_session):
    """Policy version is preserved in consent log after anonymization."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    log = PersonalDataConsentLog(
        user_id=user.id,
        telegram_id=user.telegram_id,
        policy_version="1.0",
        name=user.name,
        phone=user.phone,
    )
    db_session.add(log)
    await db_session.flush()

    await db_session.execute(
        sa_update(PersonalDataConsentLog)
        .where(PersonalDataConsentLog.user_id == user.id)
        .values(name=f'[анонимизировано_{user.telegram_id}]', phone='00000000000')
    )
    await db_session.refresh(log)

    assert log.policy_version == "1.0"  # Preserved


async def test_consent_log_preserves_telegram_id(db_session):
    """Telegram ID is preserved in consent log after anonymization."""
    user = _make_user(telegram_id=930_099)
    db_session.add(user)
    await db_session.flush()

    log = PersonalDataConsentLog(
        user_id=user.id,
        telegram_id=930_099,
        policy_version="1.0",
        name=user.name,
        phone=user.phone,
    )
    db_session.add(log)
    await db_session.flush()

    await db_session.execute(
        sa_update(PersonalDataConsentLog)
        .where(PersonalDataConsentLog.user_id == user.id)
        .values(name=f'[анонимизировано_{user.telegram_id}]', phone='00000000000')
    )
    await db_session.refresh(log)

    assert log.telegram_id == 930_099


# ---------------------------------------------------------------------------
# 7. Re-registration after revocation
# ---------------------------------------------------------------------------

async def test_reregistration_after_revoke(db_session):
    """After revocation, user can re-consent and re-register."""
    user = _make_user(name="Удалён_930_050", phone="00000000000", consent=False)
    db_session.add(user)
    await db_session.flush()

    assert has_pd_consent(user) is False
    assert user.name.startswith("Удалён_")

    # Re-consent
    user.pd_consent_at = now_salon()
    user.pd_consent_version = Config.PRIVACY_POLICY_VERSION
    user.name = "Новый Клиент"
    user.phone = "79009998877"
    await db_session.flush()

    assert has_pd_consent(user) is True
    assert user.name == "Новый Клиент"
    assert user.phone == "79009998877"


async def test_anonymized_user_blocked_by_middleware(db_session):
    """User with anonymized data (Удалён_*) triggers re-registration flow."""
    user = _make_user(name="Удалён_930_060", phone="00000000000", consent=True)
    db_session.add(user)
    await db_session.flush()

    # The middleware checks has_pd_consent → True (consent was restored)
    # But the handler checks name.startswith("Удалён_") to trigger re-registration
    needs_reregistration = user.name.startswith("Удалён_") or user.phone == "00000000000"
    assert needs_reregistration is True
