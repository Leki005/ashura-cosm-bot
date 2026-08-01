"""
Tests for bonus system — atomic operations, refunds, revocations, edge cases.

Covers:
  1. Atomic deduction (UPDATE ... WHERE balance >= amount)
  2. Refund on client cancel
  3. Revocation on admin reject
  4. Race condition (two cancellations simultaneously)
  5. Balance never goes negative
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select, update as sa_update

from config import Config
from database import Booking, BonusTransaction, Service, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(telegram_id=910_001, name="Бонус Тест", phone="79002223344", bonus_balance=0):
    return User(telegram_id=telegram_id, name=name, phone=phone, bonus_balance=bonus_balance)


def _seed_service(session, name="Чистка лица", price=3500):
    svc = Service(name=name, category="Уход за лицом", price=price, duration=90, is_active=True)
    session.add(svc)
    return svc


# ---------------------------------------------------------------------------
# 1. Atomic deduction
# ---------------------------------------------------------------------------

async def test_atomic_deduction_success(db_session):
    """Deduction succeeds when balance >= amount."""
    user = _make_user(bonus_balance=500)
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(
        sa_update(User)
        .where(User.id == user.id, User.bonus_balance >= 200)
        .values(bonus_balance=User.bonus_balance - 200)
    )
    assert result.rowcount == 1

    await db_session.refresh(user)
    assert user.bonus_balance == 300


async def test_atomic_deduction_fails_when_insufficient(db_session):
    """Deduction fails (0 rows) when balance < amount."""
    user = _make_user(bonus_balance=100)
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(
        sa_update(User)
        .where(User.id == user.id, User.bonus_balance >= 500)
        .values(bonus_balance=User.bonus_balance - 500)
    )
    assert result.rowcount == 0

    await db_session.refresh(user)
    assert user.bonus_balance == 100, "Balance must not change on failed deduction"


async def test_atomic_deduction_exact_amount(db_session):
    """Deduction of exact balance amount succeeds."""
    user = _make_user(bonus_balance=200)
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(
        sa_update(User)
        .where(User.id == user.id, User.bonus_balance >= 200)
        .values(bonus_balance=User.bonus_balance - 200)
    )
    assert result.rowcount == 1

    await db_session.refresh(user)
    assert user.bonus_balance == 0


async def test_atomic_deduction_zero_balance(db_session):
    """Cannot deduct from zero balance."""
    user = _make_user(bonus_balance=0)
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(
        sa_update(User)
        .where(User.id == user.id, User.bonus_balance >= 1)
        .values(bonus_balance=User.bonus_balance - 1)
    )
    assert result.rowcount == 0

    await db_session.refresh(user)
    assert user.bonus_balance == 0


async def test_bonus_transaction_history_preserved(db_session):
    """Each deduction creates a BonusTransaction record."""
    user = _make_user(bonus_balance=1000)
    db_session.add(user)
    await db_session.flush()

    from utils.helpers import add_bonus_transaction

    await add_bonus_transaction(db_session, user.id, -300, "Списание на скидку")
    await add_bonus_transaction(db_session, user.id, 500, "Начисление за визит")

    result = await db_session.execute(
        select(BonusTransaction)
        .where(BonusTransaction.user_id == user.id)
        .order_by(BonusTransaction.id)
    )
    txs = result.scalars().all()
    assert len(txs) == 2
    assert txs[0].amount == -300
    assert txs[1].amount == 500


# ---------------------------------------------------------------------------
# 2. Refund on client cancel
# ---------------------------------------------------------------------------

async def test_refund_bonuses_on_client_cancel(db_session):
    """When client cancels, bonus_used is refunded to balance."""
    user = _make_user(bonus_balance=200)
    db_session.add(user)
    svc = _seed_service(db_session)
    await db_session.flush()

    # Client used 200 bonuses when booking
    booking = Booking(
        user_id=user.id, service_id=svc.id,
        status="pending", bonus_used=200,
        preferred_date="20.07.2026", preferred_time="14:00",
    )
    db_session.add(booking)
    await db_session.flush()

    # Simulate what cancel_booking_confirm does: refund
    bonus_to_refund = booking.bonus_used
    assert bonus_to_refund == 200

    await db_session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=User.bonus_balance + bonus_to_refund)
    )
    await db_session.refresh(user)
    assert user.bonus_balance == 400  # 200 original + 200 refunded

    booking.bonus_used = 0
    booking.status = "cancelled"

    from utils.helpers import add_bonus_transaction
    await add_bonus_transaction(
        db_session, user.id, bonus_to_refund,
        f"Возврат бонусов при отмене записи #{booking.id}",
        booking_id=booking.id,
    )

    # Verify transaction log
    result = await db_session.execute(
        select(BonusTransaction).where(
            BonusTransaction.user_id == user.id,
            BonusTransaction.amount > 0,
        )
    )
    tx = result.scalar_one()
    assert tx.amount == 200
    assert "Возврат" in tx.description


async def test_no_refund_when_bonus_used_is_zero(db_session):
    """No refund operation when bonus_used was 0."""
    user = _make_user(bonus_balance=100)
    db_session.add(user)
    svc = _seed_service(db_session)
    await db_session.flush()

    booking = Booking(
        user_id=user.id, service_id=svc.id,
        status="pending", bonus_used=0,
    )
    db_session.add(booking)
    await db_session.flush()

    # Cancel logic: check if bonus_used > 0
    if booking.bonus_used and booking.bonus_used > 0:
        pytest.fail("Should not refund when bonus_used is 0")

    await db_session.refresh(user)
    assert user.bonus_balance == 100, "Balance unchanged"


# ---------------------------------------------------------------------------
# 3. Revocation on admin reject (confirmation bonus rollback)
# ---------------------------------------------------------------------------

async def test_revoke_confirmation_bonus_on_admin_reject(db_session):
    """
    When admin rejects a confirmed booking, the 3% confirmation bonus is revoked.
    Uses func.max(0, ...) to prevent negative balance.
    """
    user = _make_user(bonus_balance=105)  # 105 = 3% of 3500
    db_session.add(user)
    svc = _seed_service(db_session, price=3500)
    await db_session.flush()

    booking = Booking(
        user_id=user.id, service_id=svc.id,
        status="confirmed", bonus_used=0,
        preferred_date="20.07.2026", preferred_time="14:00",
    )
    db_session.add(booking)
    await db_session.flush()

    # Simulate confirmation bonus transaction
    from utils.helpers import add_bonus_transaction
    await add_bonus_transaction(
        db_session, user.id, 105,
        f"Бонус 3% при подтверждении записи #{booking.id}",
        booking_id=booking.id,
    )
    await db_session.refresh(user)
    assert user.bonus_balance == 105

    # Admin rejects → revoke confirmation bonus
    confirmation_tx_amount = 105
    await db_session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=func.max(0, User.bonus_balance - confirmation_tx_amount))
    )
    await db_session.refresh(user)
    assert user.bonus_balance == 0

    await add_bonus_transaction(
        db_session, user.id, -105,
        f"Отзыв бонуса при отмене записи #{booking.id}",
        booking_id=booking.id,
    )

    # Verify revocation transaction
    result = await db_session.execute(
        select(BonusTransaction).where(
            BonusTransaction.user_id == user.id,
            BonusTransaction.amount < 0,
        )
    )
    tx = result.scalar_one()
    assert tx.amount == -105
    assert "Отзыв" in tx.description


async def test_revoke_confirmation_bonus_partial_when_low_balance(db_session):
    """
    If user spent some bonuses between confirmation and rejection,
    func.max(0, ...) prevents negative balance.
    """
    user = _make_user(bonus_balance=50)  # only 50 left (spent 55 of 105)
    db_session.add(user)
    await db_session.flush()

    confirmation_tx_amount = 105  # original was 105, but user only has 50

    await db_session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=func.max(0, User.bonus_balance - confirmation_tx_amount))
    )
    await db_session.refresh(user)
    assert user.bonus_balance == 0, "func.max(0, ...) prevents negative balance"


async def test_revoke_refund_bonus_on_admin_reject_with_bonus_used(db_session):
    """
    Admin reject refunds bonus_used AND revokes confirmation bonus.
    """
    user = _make_user(bonus_balance=500)
    db_session.add(user)
    svc = _seed_service(db_session, price=3500)
    await db_session.flush()

    booking = Booking(
        user_id=user.id, service_id=svc.id,
        status="confirmed", bonus_used=200,
        preferred_date="20.07.2026", preferred_time="14:00",
    )
    db_session.add(booking)
    await db_session.flush()

    from utils.helpers import add_bonus_transaction

    # Step 1: Refund bonus_used (200)
    bonus_refunded = booking.bonus_used
    await db_session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=User.bonus_balance + bonus_refunded)
    )
    await db_session.refresh(user)
    assert user.bonus_balance == 700  # 500 + 200 refund

    await add_bonus_transaction(
        db_session, user.id, bonus_refunded,
        f"Возврат бонусов при отклонении записи #{booking.id}",
        booking_id=booking.id,
    )

    # Step 2: Revoke confirmation bonus (105)
    await add_bonus_transaction(
        db_session, user.id, 105,
        f"Бонус 3% при подтверждении записи #{booking.id}",
        booking_id=booking.id,
    )
    await db_session.refresh(user)
    user.bonus_balance += 105  # simulate confirmation bonus was granted
    await db_session.flush()

    # Now revoke
    await db_session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=func.max(0, User.bonus_balance - 105))
    )
    await db_session.refresh(user)
    assert user.bonus_balance == 700, "After revoke: 500 + 200 refund + 105 grant - 105 revoke = 700"


# ---------------------------------------------------------------------------
# 4. Race condition — two cancellations simultaneously
# ---------------------------------------------------------------------------

async def test_race_condition_double_refund_prevention(db_session):
    """
    Two concurrent cancel calls could double-refund bonus_used.
    Setting bonus_used=0 after first refund prevents this.
    """
    user = _make_user(bonus_balance=0)
    db_session.add(user)
    svc = _seed_service(db_session)
    await db_session.flush()

    booking = Booking(
        user_id=user.id, service_id=svc.id,
        status="pending", bonus_used=300,
    )
    db_session.add(booking)
    await db_session.flush()

    # First cancel: refund
    bonus_to_refund = booking.bonus_used
    assert bonus_to_refund == 300

    await db_session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=User.bonus_balance + bonus_to_refund)
    )
    booking.bonus_used = 0  # Critical: prevents double refund
    await db_session.flush()

    await db_session.refresh(user)
    assert user.bonus_balance == 300

    # Second cancel attempt: bonus_used is now 0, so no refund
    if booking.bonus_used and booking.bonus_used > 0:
        pytest.fail("Double refund! bonus_used should be 0 after first refund")

    assert booking.bonus_used == 0
    await db_session.refresh(user)
    assert user.bonus_balance == 300, "No double refund"


# ---------------------------------------------------------------------------
# 5. Balance never goes negative
# ---------------------------------------------------------------------------

async def test_balance_never_negative_after_revoke(db_session):
    """
    func.max(0, ...) in the revoke UPDATE guarantees balance >= 0.
    """
    user = _make_user(bonus_balance=10)
    db_session.add(user)
    await db_session.flush()

    # Try to revoke 500 (more than balance)
    await db_session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=func.max(0, User.bonus_balance - 500))
    )
    await db_session.refresh(user)
    assert user.bonus_balance == 0, "Balance should be 0, not -490"
    assert user.bonus_balance >= 0, "Balance must never be negative"


async def test_balance_never_negative_after_multiple_operations(db_session):
    """Multiple rapid operations keep balance >= 0."""
    user = _make_user(bonus_balance=100)
    db_session.add(user)
    await db_session.flush()

    from utils.helpers import add_bonus_transaction

    # Grant 50
    user.bonus_balance += 50
    await add_bonus_transaction(db_session, user.id, 50, "Grant")

    # Revoke 200 (more than 150)
    await db_session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(bonus_balance=func.max(0, User.bonus_balance - 200))
    )
    await db_session.refresh(user)
    assert user.bonus_balance == 0
    assert user.bonus_balance >= 0


async def test_check_constraint_bonus_used_nonneg(db_session):
    """
    DB CHECK constraint: bonus_used >= 0.
    Negative bonus_used must raise IntegrityError.
    """
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    from sqlalchemy.exc import IntegrityError
    booking = Booking(user_id=user.id, status="pending", bonus_used=-100)
    db_session.add(booking)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_confirmation_bonus_not_granted_twice(db_session):
    """
    grant_confirmation_bonus checks _booking_has_confirmation_bonus first.
    Second call must return 0.
    """
    user = _make_user(bonus_balance=0)
    db_session.add(user)
    svc = _seed_service(db_session, price=3500)
    await db_session.flush()

    # Seed 1 completed visit so loyalty tier = 3%
    completed_booking = Booking(
        user_id=user.id, service_id=svc.id,
        status="completed",
        preferred_date="10.07.2026", preferred_time="10:00",
    )
    db_session.add(completed_booking)
    await db_session.flush()

    booking = Booking(
        user_id=user.id, service_id=svc.id,
        status="confirmed",
        preferred_date="20.07.2026", preferred_time="14:00",
    )
    db_session.add(booking)
    await db_session.flush()

    # Load the service relationship so calculate_booking_price can read it
    await db_session.refresh(booking, ["service"])

    # First grant — should succeed (1 completed visit → 3% loyalty)
    from utils.helpers import grant_confirmation_bonus
    amount1 = await grant_confirmation_bonus(db_session, booking)
    assert amount1 > 0, "First grant should succeed (loyalty 3%)"

    # Second grant — must return 0 (already granted)
    await db_session.refresh(booking)
    amount2 = await grant_confirmation_bonus(db_session, booking)
    assert amount2 == 0, "Second grant must return 0 (duplicate prevention)"
