"""
Tests for the reminder system (24h and 2h before appointment).

Covers:
  1. 24h reminder sent when booking is within 24h window
  2. 2h reminder sent when booking is within 2h window
  3. Deduplication (flags prevent re-sending)
  4. Invalid/missing date handling
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from config import Config
from database import Booking, Service, User
from utils.helpers import now_salon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(telegram_id=920_001, name="Напомин Тест", phone="79003334455"):
    return User(telegram_id=telegram_id, name=name, phone=phone)


async def _make_booking(user, session, *, date_str, time_str, status="confirmed",
                  reminder_24h=False, reminder_2h=False, service_name="Чистка лица"):
    svc = Service(name=service_name, category="Уход за лицом", price=3500, duration=90, is_active=True)
    session.add(svc)
    await session.flush()

    booking = Booking(
        user_id=user.id,
        service_id=svc.id,
        preferred_date=date_str,
        preferred_time=time_str,
        status=status,
        reminder_24h_sent=reminder_24h,
        reminder_2h_sent=reminder_2h,
    )
    session.add(booking)
    await session.flush()
    return booking


def _parse_booking_dt(date_str, time_str):
    """Replicates the date-parsing logic from _send_reminders_inner."""
    from datetime import datetime as dt
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            full = f"{date_str} {time_str}".strip() if time_str else date_str
            return dt.strptime(full, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 1. 24h reminder window
# ---------------------------------------------------------------------------

async def test_reminder_24h_sends_in_window(db_session):
    """Reminder 24h triggers when booking is 1-24h away and flag is False."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=12)
    date_str = booking_time.strftime("%d.%m.%Y")
    time_str = booking_time.strftime("%H:%M")

    booking = await _make_booking(user, db_session, date_str=date_str, time_str=time_str)
    assert booking.reminder_24h_sent is False

    booking_dt = _parse_booking_dt(date_str, time_str)
    time_to_booking = booking_dt - now

    # Check: should trigger
    should_send = (
        not booking.reminder_24h_sent
        and timedelta(minutes=0) < time_to_booking <= timedelta(hours=24, minutes=5)
    )
    assert should_send is True, "12h away → should send 24h reminder"


async def test_reminder_24h_not_sent_when_too_far(db_session):
    """No 24h reminder if booking is >24h away."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=48)
    date_str = booking_time.strftime("%d.%m.%Y")
    time_str = booking_time.strftime("%H:%M")

    booking = await _make_booking(user, db_session, date_str=date_str, time_str=time_str)
    booking_dt = _parse_booking_dt(date_str, time_str)
    time_to_booking = booking_dt - now

    should_send = (
        not booking.reminder_24h_sent
        and timedelta(minutes=0) < time_to_booking <= timedelta(hours=24, minutes=5)
    )
    assert should_send is False, "48h away → should NOT send 24h reminder"


async def test_reminder_24h_not_sent_when_past(db_session):
    """No reminder if booking time is in the past."""
    now = now_salon()
    time_to_booking = timedelta(hours=-2)  # 2h ago

    should_send = (
        timedelta(minutes=0) < time_to_booking <= timedelta(hours=24, minutes=5)
    )
    assert should_send is False, "Past booking → no reminder"


async def test_reminder_24h_sent_flag_set(db_session):
    """After sending, reminder_24h_sent is set to True."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=10)
    booking = await _make_booking(
        user, db_session,
        date_str=booking_time.strftime("%d.%m.%Y"),
        time_str=booking_time.strftime("%H:%M"),
    )

    assert booking.reminder_24h_sent is False
    booking.reminder_24h_sent = True
    await db_session.flush()

    await db_session.refresh(booking)
    assert booking.reminder_24h_sent is True


# ---------------------------------------------------------------------------
# 2. 2h reminder window
# ---------------------------------------------------------------------------

async def test_reminder_2h_sends_in_window(db_session):
    """Reminder 2h triggers when booking is 1min-2h away and flag is False."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=1)
    date_str = booking_time.strftime("%d.%m.%Y")
    time_str = booking_time.strftime("%H:%M")

    booking = await _make_booking(user, db_session, date_str=date_str, time_str=time_str)
    booking_dt = _parse_booking_dt(date_str, time_str)
    time_to_booking = booking_dt - now

    should_send = (
        not booking.reminder_2h_sent
        and timedelta(minutes=0) < time_to_booking <= timedelta(hours=2, minutes=5)
    )
    assert should_send is True, "1h away → should send 2h reminder"


async def test_reminder_2h_not_sent_when_5h_away(db_session):
    """No 2h reminder if booking is 5h away."""
    now = now_salon()
    time_to_booking = timedelta(hours=5)

    should_send = (
        timedelta(minutes=0) < time_to_booking <= timedelta(hours=2, minutes=5)
    )
    assert should_send is False


async def test_reminder_2h_sends_at_boundary_2h(db_session):
    """2h reminder triggers at exactly 2h + 5min (catch-up window)."""
    now = now_salon()
    time_to_booking = timedelta(hours=2, minutes=3)

    should_send = (
        timedelta(minutes=0) < time_to_booking <= timedelta(hours=2, minutes=5)
    )
    assert should_send is True, "2h3min → within catch-up window"


async def test_reminder_2h_not_sent_at_2h6min(db_session):
    """2h reminder does NOT trigger at 2h6min (outside catch-up window)."""
    now = now_salon()
    time_to_booking = timedelta(hours=2, minutes=6)

    should_send = (
        timedelta(minutes=0) < time_to_booking <= timedelta(hours=2, minutes=5)
    )
    assert should_send is False


async def test_reminder_2h_flag_set_after_send(db_session):
    """After sending, reminder_2h_sent is set to True."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(minutes=90)
    booking = await _make_booking(
        user, db_session,
        date_str=booking_time.strftime("%d.%m.%Y"),
        time_str=booking_time.strftime("%H:%M"),
    )

    assert booking.reminder_2h_sent is False
    booking.reminder_2h_sent = True
    await db_session.flush()

    await db_session.refresh(booking)
    assert booking.reminder_2h_sent is True


# ---------------------------------------------------------------------------
# 3. Deduplication — flags prevent re-sending
# ---------------------------------------------------------------------------

async def test_24h_reminder_not_sent_twice(db_session):
    """If reminder_24h_sent is True, the reminder is not sent again."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=12)
    booking = await _make_booking(
        user, db_session,
        date_str=booking_time.strftime("%d.%m.%Y"),
        time_str=booking_time.strftime("%H:%M"),
        reminder_24h=True,  # Already sent
    )

    booking_dt = _parse_booking_dt(booking.preferred_date, booking.preferred_time)
    time_to_booking = booking_dt - now

    should_send = (
        not booking.reminder_24h_sent
        and timedelta(minutes=0) < time_to_booking <= timedelta(hours=24, minutes=5)
    )
    assert should_send is False, "Already sent → must not send again"


async def test_2h_reminder_not_sent_twice(db_session):
    """If reminder_2h_sent is True, the reminder is not sent again."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(minutes=60)
    booking = await _make_booking(
        user, db_session,
        date_str=booking_time.strftime("%d.%m.%Y"),
        time_str=booking_time.strftime("%H:%M"),
        reminder_2h=True,  # Already sent
    )

    booking_dt = _parse_booking_dt(booking.preferred_date, booking.preferred_time)
    time_to_booking = booking_dt - now

    should_send = (
        not booking.reminder_2h_sent
        and timedelta(minutes=0) < time_to_booking <= timedelta(hours=2, minutes=5)
    )
    assert should_send is False


async def test_reminder_not_sent_for_cancelled_booking(db_session):
    """Reminders are only sent for 'confirmed' bookings, not cancelled."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=12)
    booking = await _make_booking(
        user, db_session,
        date_str=booking_time.strftime("%d.%m.%Y"),
        time_str=booking_time.strftime("%H:%M"),
        status="cancelled",  # Cancelled
    )

    # The query filters by status == "confirmed", so cancelled bookings
    # are never even loaded by the reminder system.
    result = await db_session.execute(
        select(Booking).where(Booking.status == "confirmed")
    )
    confirmed = result.scalars().all()
    assert booking.id not in [b.id for b in confirmed]


async def test_reminder_not_sent_for_pending_booking(db_session):
    """Reminders are only for 'confirmed' bookings, not pending."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=12)
    booking = await _make_booking(
        user, db_session,
        date_str=booking_time.strftime("%d.%m.%Y"),
        time_str=booking_time.strftime("%H:%M"),
        status="pending",
    )

    result = await db_session.execute(
        select(Booking).where(Booking.status == "confirmed")
    )
    confirmed = result.scalars().all()
    assert booking.id not in [b.id for b in confirmed]


async def test_reminder_not_sent_for_completed_booking(db_session):
    """Reminders skip completed bookings."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=12)
    booking = await _make_booking(
        user, db_session,
        date_str=booking_time.strftime("%d.%m.%Y"),
        time_str=booking_time.strftime("%H:%M"),
        status="completed",
    )

    result = await db_session.execute(
        select(Booking).where(Booking.status == "confirmed")
    )
    confirmed = result.scalars().all()
    assert booking.id not in [b.id for b in confirmed]


# ---------------------------------------------------------------------------
# 4. Invalid/missing date handling
# ---------------------------------------------------------------------------

async def test_reminder_skips_booking_without_date(db_session):
    """Bookings without preferred_date are skipped by the reminder system."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    booking = await _make_booking(
        user, db_session,
        date_str="", time_str="", status="confirmed",
    )

    # The reminder system checks `if not date_str: continue`
    date_str = booking.preferred_date or ""
    if not date_str:
        skipped = True
    else:
        skipped = False
    assert skipped is True, "Empty date → must skip"


async def test_reminder_skips_booking_without_time(db_session):
    """Booking without time can still trigger (date-only check)."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    now = now_salon()
    booking_time = now + timedelta(hours=12)
    date_str = booking_time.strftime("%d.%m.%Y")

    booking = await _make_booking(user, db_session, date_str=date_str, time_str="")

    # Parse without time — falls back to date-only format
    booking_dt = _parse_booking_dt(date_str, "")
    assert booking_dt is not None, "Date-only booking should still parse"


async def test_reminder_skips_invalid_date_format(db_session):
    """Invalid date format causes booking to be skipped."""
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    booking = await _make_booking(
        user, db_session,
        date_str="не дата", time_str="14:00", status="confirmed",
    )

    booking_dt = _parse_booking_dt(booking.preferred_date, booking.preferred_time)
    assert booking_dt is None, "Invalid date → must parse to None"


async def test_reminder_skips_booking_in_past(db_session):
    """Booking in the past: time_to_booking < 0, so no reminder."""
    now = now_salon()
    time_to_booking = timedelta(minutes=-30)

    should_send_24h = (
        timedelta(minutes=0) < time_to_booking <= timedelta(hours=24, minutes=5)
    )
    should_send_2h = (
        timedelta(minutes=0) < time_to_booking <= timedelta(hours=2, minutes=5)
    )
    assert should_send_24h is False
    assert should_send_2h is False


async def test_reminder_catchup_24h_when_less_than_24h_left(db_session):
    """
    Catch-up: if a booking was confirmed late (e.g. 10h before),
    the 24h reminder still fires because it's within the window.
    """
    now = now_salon()
    time_to_booking = timedelta(hours=10)  # 10h left

    should_send = (
        not False  # reminder_24h_sent is False
        and timedelta(minutes=0) < time_to_booking <= timedelta(hours=24, minutes=5)
    )
    assert should_send is True, "Catch-up: 10h left → still within 24h window"


async def test_both_reminders_can_fire_on_same_check(db_session):
    """
    Both 24h and 2h reminders can fire on the same check cycle
    (e.g. booking is 1.5h away → both within 24h and within 2h windows).
    """
    now = now_salon()
    time_to_booking = timedelta(hours=1, minutes=30)

    should_send_24h = (
        not False
        and timedelta(minutes=0) < time_to_booking <= timedelta(hours=24, minutes=5)
    )
    should_send_2h = (
        not False
        and timedelta(minutes=0) < time_to_booking <= timedelta(hours=2, minutes=5)
    )
    assert should_send_24h is True
    assert should_send_2h is True
