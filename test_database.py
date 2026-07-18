import pytest
from sqlalchemy.exc import IntegrityError

from database import (
    BonusTransaction,
    Booking,
    PersonalDataConsentLog,
    Review,
    Service,
    User,
)


# ---------------------------------------------------------------------------
# 1. User
# ---------------------------------------------------------------------------

async def test_user_creation(db_session):
    user = User(telegram_id=123456, name="Анна", phone="+79001234567")
    db_session.add(user)
    await db_session.commit()

    assert user.id is not None
    assert user.telegram_id == 123456
    assert user.name == "Анна"
    assert user.phone == "+79001234567"
    assert user.bonus_balance == 0
    assert user.created_at is not None


# ---------------------------------------------------------------------------
# 2. Booking
# ---------------------------------------------------------------------------

async def test_booking_creation(db_session):
    user = User(telegram_id=200, name="Борис", phone="+79009990000")
    db_session.add(user)
    await db_session.flush()

    booking = Booking(user_id=user.id, preferred_date="2026-07-20", preferred_time="14:00")
    db_session.add(booking)
    await db_session.commit()

    assert booking.id is not None
    assert booking.status == "pending"
    assert booking.bonus_used == 0


async def test_booking_relationships(db_session):
    user = User(telegram_id=201, name="Вера", phone="+79008880000")
    db_session.add(user)
    await db_session.flush()

    booking = Booking(user_id=user.id)
    db_session.add(booking)
    await db_session.commit()

    await db_session.refresh(booking, ["user"])
    await db_session.refresh(user, ["bookings"])

    assert booking.user.id == user.id
    assert len(user.bookings) == 1
    assert user.bookings[0].id == booking.id


# ---------------------------------------------------------------------------
# 3. Service
# ---------------------------------------------------------------------------

async def test_service_creation(db_session):
    svc = Service(name="Чистка лица", category="Уход за лицом", price=3500, duration=90)
    db_session.add(svc)
    await db_session.commit()

    assert svc.id is not None
    assert svc.name == "Чистка лица"
    assert svc.category == "Уход за лицом"
    assert svc.price == 3500
    assert svc.is_active is True


# ---------------------------------------------------------------------------
# 4. Review
# ---------------------------------------------------------------------------

async def test_review_creation(db_session):
    user = User(telegram_id=300, name="Галина", phone="+79007770000")
    db_session.add(user)
    await db_session.flush()

    review = Review(user_id=user.id, rating=5, text="Отлично!")
    db_session.add(review)
    await db_session.commit()

    assert review.id is not None
    assert review.rating == 5
    assert review.is_published is False


@pytest.mark.parametrize("bad_rating", [0, 6])
async def test_review_rating_constraint(db_session, bad_rating):
    user = User(telegram_id=301, name="Дмитрий", phone="+79006660000")
    db_session.add(user)
    await db_session.flush()

    review = Review(user_id=user.id, rating=bad_rating)
    db_session.add(review)

    with pytest.raises(IntegrityError):
        await db_session.commit()


# ---------------------------------------------------------------------------
# 5. BonusTransaction
# ---------------------------------------------------------------------------

async def test_bonus_transaction_credit(db_session):
    user = User(telegram_id=400, name="Елена", phone="+79005550000", bonus_balance=500)
    db_session.add(user)
    await db_session.flush()

    tx = BonusTransaction(user_id=user.id, amount=500, description="Начисление за визит")
    db_session.add(tx)
    await db_session.commit()

    assert tx.id is not None
    assert tx.amount == 500


async def test_bonus_transaction_debit(db_session):
    user = User(telegram_id=401, name="Жанна", phone="+79004440000")
    db_session.add(user)
    await db_session.flush()

    tx = BonusTransaction(user_id=user.id, amount=-200, description="Списание на скидку")
    db_session.add(tx)
    await db_session.commit()

    assert tx.amount == -200


# ---------------------------------------------------------------------------
# 6. PersonalDataConsentLog
# ---------------------------------------------------------------------------

async def test_pd_consent_log_creation(db_session):
    user = User(telegram_id=500, name="Зина", phone="+79003330000")
    db_session.add(user)
    await db_session.flush()

    log = PersonalDataConsentLog(
        user_id=user.id,
        telegram_id=500,
        policy_version="1.0",
        name="Зина",
        phone="+79003330000",
    )
    db_session.add(log)
    await db_session.commit()

    assert log.id is not None
    assert log.user_id == user.id
    assert log.telegram_id == 500
    assert log.policy_version == "1.0"
    assert log.name == "Зина"
    assert log.phone == "+79003330000"
    assert log.consented_at is not None


# ---------------------------------------------------------------------------
# 7. Unique partial index — one active booking per user
# ---------------------------------------------------------------------------

async def test_unique_active_booking_index(db_session):
    user = User(telegram_id=600, name="Ирина", phone="+79002220000")
    db_session.add(user)
    await db_session.flush()

    # First active (pending) booking — ok
    b1 = Booking(user_id=user.id, status="pending")
    db_session.add(b1)
    await db_session.commit()

    # Second active booking — must raise IntegrityError
    b2 = Booking(user_id=user.id, status="confirmed")
    db_session.add(b2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    await db_session.refresh(user)

    # Cancelled booking for the same user — must succeed
    b3 = Booking(user_id=user.id, status="cancelled")
    db_session.add(b3)
    await db_session.commit()
    assert b3.id is not None
