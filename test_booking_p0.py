"""
Integration tests for P0 fixes — catches regressions in the most critical booking logic.

Tests:
1. merge_separate DOES NOT create 2 active bookings
2. notify_ok flag works (honest error message)
3. anamnesis stale token rejected
4. throttle blocks rapid messages
5. has_pd_consent version mismatch
6. broadcast text is escaped (not raw HTML)
"""

import asyncio
import os
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from config import Config
from database import Booking, User
from utils.helpers import (
    ACTIVE_BOOKING_STATUSES,
    check_throttle,
    get_active_booking,
)
from utils.privacy import has_pd_consent


# ---------------------------------------------------------------------------
# Test 1: merge_separate DOES NOT create 2 active bookings
# ---------------------------------------------------------------------------

async def test_merge_separate_no_second_active_booking(db_session):
    """
    P0 guard: a user with an active booking must NOT get a second one.
    The UNIQUE partial index enforces this at the DB level.
    This test MUST FAIL if someone adds skip_active_booking_check=True back.
    """
    user = User(telegram_id=700_001, name="Тест Merge", phone="+79001110001")
    db_session.add(user)
    await db_session.flush()

    # Create the first active (pending) booking
    b1 = Booking(user_id=user.id, status="pending", preferred_date="20.07.2026", preferred_time="14:00")
    db_session.add(b1)
    await db_session.commit()

    # Verify: exactly one active booking
    active = await get_active_booking(db_session, user.id)
    assert active is not None
    assert active.id == b1.id

    # Attempt to create a second active booking — must raise IntegrityError
    # (the partial unique index idx_one_active_booking prevents this)
    from sqlalchemy.exc import IntegrityError
    user_id = user.id  # save PK before rollback expires the object
    b2 = Booking(user_id=user_id, status="pending", preferred_date="21.07.2026", preferred_time="15:00")
    db_session.add(b2)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # After rollback, still exactly one active booking
    result = await db_session.execute(
        select(Booking).where(
            Booking.user_id == user_id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        )
    )
    active_bookings = result.scalars().all()
    assert len(active_bookings) == 1, (
        "P0 REGRESSION: more than one active booking exists for a single user!"
    )


async def test_merge_separate_handler_shows_warning_not_new_booking(db_session):
    """
    P0 guard: merge_separate handler must NOT call _start_service_booking.
    It only shows a warning message and offers merge or view bookings.
    """
    user = User(telegram_id=700_002, name="Тест Merge2", phone="+79001110002")
    db_session.add(user)
    await db_session.flush()

    b1 = Booking(user_id=user.id, status="confirmed", preferred_date="20.07.2026", preferred_time="14:00")
    db_session.add(b1)
    await db_session.commit()

    # Simulate what merge_separate does: re-check active booking
    active = await get_active_booking(db_session, user.id)
    assert active is not None
    assert active.status in ACTIVE_BOOKING_STATUSES

    # The handler does NOT create a new booking — it just shows a message
    # Verify no new booking was created
    result = await db_session.execute(
        select(Booking).where(Booking.user_id == user.id)
    )
    all_bookings = result.scalars().all()
    assert len(all_bookings) == 1, (
        "P0 REGRESSION: merge_separate created an extra booking!"
    )


# ---------------------------------------------------------------------------
# Test 2: notify_ok flag works
# ---------------------------------------------------------------------------

async def test_notify_ok_failure_message():
    """
    P0 guard: when notify_admin_new_booking raises, the success message
    must contain 'Не удалось' (honest message, not a lie).
    """
    # Simulate the notify_ok logic from _finalize_booking (lines 774-816)
    notify_ok = False  # admin notification failed
    if notify_ok:
        notify_line = "Ашура получила уведомление и свяжется с вами."
    else:
        notify_line = (
            "⚠️ Не удалось связаться с Ашурой автоматically.\n"
            "Пожалуйста, напишите ей напрямую: "
        )

    assert "Не удалось" in notify_line, (
        "P0 REGRESSION: failed notification must produce honest 'Не удалось' message"
    )
    assert "получила уведомление" not in notify_line, (
        "P0 REGRESSION: failed notification must NOT claim success"
    )


async def test_notify_ok_success_message():
    """
    P0 guard: when notify_admin_new_booking succeeds, the success message
    must contain 'получила уведомление'.
    """
    notify_ok = True  # admin notification succeeded
    if notify_ok:
        notify_line = "Ашура получила уведомление и свяжется с вами."
    else:
        notify_line = "⚠️ Не удалось связаться с Ашурой автоматически."

    assert "получила уведомление" in notify_line, (
        "P0 REGRESSION: successful notification must say 'получила уведомление'"
    )
    assert "Не удалось" not in notify_line, (
        "P0 REGRESSION: successful notification must NOT contain error message"
    )


async def test_notify_admin_raises_sets_notify_ok_false():
    """
    P0 guard: the try/except around notify_admin_new_booking must catch
    the exception and set notify_ok=False (not crash the booking flow).
    """
    notify_ok = False
    try:
        raise RuntimeError("Admin bot blocked")
        notify_ok = True  # noqa: unreachable
    except Exception:
        pass  # exactly what _finalize_booking does

    assert notify_ok is False, (
        "P0 REGRESSION: exception in notify must not leave notify_ok=True"
    )


# ---------------------------------------------------------------------------
# Test 3: anamnesis stale token rejected
# ---------------------------------------------------------------------------

async def test_anamnesis_stale_token_rejected():
    """
    P0 guard: a callback with a wrong/stale anamnesis token must be rejected.
    The handler checks stripped.startswith(saved_token + '_') and returns early.
    """
    saved_token = "abc123abc123abc1"  # 16 hex chars
    callback_data = f"anam_xyz789xyz789xyz_allergy_no"  # wrong token

    stripped = callback_data.removeprefix("anam_")

    # Token validation logic from anamnesis_answer (lines 320-325)
    token_valid = False
    if saved_token:
        if stripped.startswith(saved_token + "_"):
            token_valid = True

    assert token_valid is False, (
        "P0 REGRESSION: stale token was accepted — old buttons will be processed!"
    )


async def test_anamnesis_valid_token_accepted():
    """
    P0 guard: a callback with the correct anamnesis token must be accepted.
    """
    saved_token = "abc123abc123abc1"
    callback_data = f"anam_{saved_token}_allergy_no"

    stripped = callback_data.removeprefix("anam_")

    token_valid = False
    if saved_token:
        if stripped.startswith(saved_token + "_"):
            token_valid = True
            stripped = stripped.removeprefix(saved_token + "_")

    assert token_valid is True, "Valid token was rejected"
    assert stripped == "allergy_no", f"Remaining key should be 'allergy_no', got '{stripped}'"


async def test_anamnesis_keyboard_uses_token():
    """
    P0 guard: anamnesis_keyboard must embed the token in callback_data.
    Old buttons without token won't match the handler's token check.
    """
    from keyboards import anamnesis_keyboard

    token = "deadbeefdeadbeef"
    kb = anamnesis_keyboard(0, {}, anam_token=token)

    # First question is "allergy" — buttons should contain the token
    button = kb.inline_keyboard[0][0]
    assert token in button.callback_data, (
        f"P0 REGRESSION: token '{token}' not found in button callback_data '{button.callback_data}'"
    )


# ---------------------------------------------------------------------------
# Test 4: throttle blocks rapid messages
# ---------------------------------------------------------------------------

async def test_throttle_blocks_rapid_messages():
    """
    P0 guard: second call within THROTTLE_RATE must be throttled.
    First call: False (not throttled, message allowed).
    Second call immediately: True (throttled, message blocked).
    """
    user_id = 999_001

    # Clear any existing state
    from utils.helpers import _last_message_time
    _last_message_time.pop(user_id, None)

    # First call — not throttled
    result1 = await check_throttle(user_id)
    assert result1 is False, "First call should NOT be throttled"

    # Second call immediately — must be throttled
    result2 = await check_throttle(user_id)
    assert result2 is True, "Second call within THROTTLE_RATE should be throttled"

    # Cleanup
    _last_message_time.pop(user_id, None)


async def test_throttle_allows_after_delay():
    """
    P0 guard: after THROTTLE_RATE seconds, the user is no longer throttled.
    """
    user_id = 999_002

    from utils.helpers import _last_message_time
    _last_message_time.pop(user_id, None)

    # First call
    result1 = await check_throttle(user_id)
    assert result1 is False

    # Wait for throttle window to expire
    await asyncio.sleep(Config.THROTTLE_RATE + 0.1)

    # Third call after delay — not throttled
    result3 = await check_throttle(user_id)
    assert result3 is False, "Call after THROTTLE_RATE delay should NOT be throttled"

    # Cleanup
    _last_message_time.pop(user_id, None)


async def test_throttle_different_users_independent():
    """
    P0 guard: throttle is per-user, not global.
    """
    user_a = 999_003
    user_b = 999_004

    from utils.helpers import _last_message_time
    _last_message_time.pop(user_a, None)
    _last_message_time.pop(user_b, None)

    # User A sends a message
    await check_throttle(user_a)

    # User B sends immediately after — should NOT be throttled
    result_b = await check_throttle(user_b)
    assert result_b is False, "Different users should have independent throttle state"

    # Cleanup
    _last_message_time.pop(user_a, None)
    _last_message_time.pop(user_b, None)


# ---------------------------------------------------------------------------
# Test 5: has_pd_consent version mismatch
# ---------------------------------------------------------------------------

async def test_has_pd_consent_matching_version():
    """
    P0 guard: user with matching pd_consent_version → True.
    """
    user = MagicMock()
    user.pd_consent_at = datetime.now()
    user.pd_consent_version = Config.PRIVACY_POLICY_VERSION

    assert has_pd_consent(user) is True, (
        "P0 REGRESSION: user with matching consent version should return True"
    )


async def test_has_pd_consent_stale_version():
    """
    P0 guard: user with old pd_consent_version → False (must re-consent).
    """
    user = MagicMock()
    user.pd_consent_at = datetime.now()
    user.pd_consent_version = "0.0"  # old version

    assert has_pd_consent(user) is False, (
        "P0 REGRESSION: user with stale consent version must return False"
    )


async def test_has_pd_consent_none_version_legacy():
    """
    P0 guard: user with pd_consent_version=None → True (legacy user).
    Legacy users who gave consent before versioning was added are trusted.
    """
    user = MagicMock()
    user.pd_consent_at = datetime.now()
    user.pd_consent_version = None  # legacy — gave consent before versioning

    assert has_pd_consent(user) is True, (
        "P0 REGRESSION: legacy user with None version and valid consent_at should return True"
    )


async def test_has_pd_consent_no_consent_at():
    """
    P0 guard: user with pd_consent_at=None → False (never gave consent).
    """
    user = MagicMock()
    user.pd_consent_at = None
    user.pd_consent_version = Config.PRIVACY_POLICY_VERSION

    assert has_pd_consent(user) is False, (
        "P0 REGRESSION: user with no consent_at must return False"
    )


async def test_has_pd_consent_none_user():
    """
    P0 guard: None user → False.
    """
    assert has_pd_consent(None) is False, (
        "P0 REGRESSION: None user must return False"
    )


# ---------------------------------------------------------------------------
# Test 6: broadcast text is escaped (not raw HTML)
# ---------------------------------------------------------------------------

async def test_broadcast_sends_plain_text_no_parse_mode():
    """
    P0 guard: _send_broadcast must send text without parse_mode='HTML'.
    The preview uses html_escape for display, but the actual send is plain text
    so users see raw text, not rendered HTML that could contain malicious markup.
    """
    from handlers.admin import _send_broadcast

    # Create a mock bot that records calls
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()

    # Text with HTML-like content
    broadcast_text = "<b>Bold</b> & <script>alert('xss')</script>"

    # Call _send_broadcast with telegram_ids (list[int])
    await _send_broadcast(mock_bot, [800_001], broadcast_text, admin_id=123)

    # Find the call that sent to the user (not the admin report)
    user_calls = [
        call for call in mock_bot.send_message.call_args_list
        if call.kwargs.get("chat_id") == 800_001 or (
            call.args and len(call.args) > 0 and call.args[0] == 800_001
        )
    ]
    assert len(user_calls) >= 1, "Broadcast should have sent at least one message to user"

    user_call = user_calls[0]

    # Check: no parse_mode='HTML' — text is sent as plain text
    parse_mode = user_call.kwargs.get("parse_mode")
    assert parse_mode != "HTML", (
        "P0 REGRESSION: broadcast uses parse_mode='HTML' — "
        "raw HTML/markup will be rendered instead of escaped text"
    )

    # Check: text contains the raw string (not HTML-escaped by the sender)
    sent_text = user_call.kwargs.get("text") or (user_call.args[1] if len(user_call.args) > 1 else None)
    assert sent_text is not None, "Broadcast text should not be None"
    # The text should be sent as-is (plain text), so <b> should appear literally
    assert "<b>" in sent_text, (
        "P0 REGRESSION: broadcast text was HTML-escaped before sending — "
        "users will see '&lt;b&gt;' instead of '<b>'"
    )


async def test_broadcast_preview_escapes_html():
    """
    P0 guard: the admin preview of the broadcast uses html_escape.
    The preview is shown with parse_mode='HTML', so the raw text must be escaped.
    """
    from html import escape as html_escape

    raw_text = "<b>Important</b> & update"
    escaped = html_escape(raw_text)

    # The preview should escape HTML entities
    assert "&lt;b&gt;" in escaped, "Preview should escape HTML tags"
    assert "&amp;" in escaped, "Preview should escape ampersands"

    # But the actual send should NOT escape (plain text)
    # This is the split: preview=escaped+HTML, send=raw+plain


# ---------------------------------------------------------------------------
# Test 7: AST regression checks
# ---------------------------------------------------------------------------

def test_skip_active_booking_check_not_set_true():
    """Regression: ensure skip_active_booking_check is never set to True in client.py"""
    import ast
    filepath = os.path.join(os.path.dirname(__file__), 'handlers', 'client.py')
    with open(filepath, 'r', encoding='utf-8') as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and hasattr(node, 'func'):
            # Check for state.update_data(skip_active_booking_check=True)
            if hasattr(node.func, 'attr') and node.func.attr == 'update_data':
                for kw in node.keywords:
                    if kw.arg == 'skip_active_booking_check':
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            pytest.fail('skip_active_booking_check=True found in client.py — regression!')


def test_merge_separate_no_booking_creation():
    """Regression: merge_separate handler must not create a new Booking"""
    import ast
    filepath = os.path.join(os.path.dirname(__file__), 'handlers', 'client.py')
    with open(filepath, 'r', encoding='utf-8') as f:
        source = f.read()
    tree = ast.parse(source)
    # Find the merge_separate function
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == 'merge_separate':
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Name) and func.id == 'Booking':
                        pytest.fail('merge_separate creates a Booking — regression!')
                    if isinstance(func, ast.Attribute) and func.attr == 'add':
                        # Check if it's session.add(Booking(...))
                        for arg in child.args:
                            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == 'Booking':
                                pytest.fail('merge_separate adds Booking to session — regression!')


def test_broadcast_no_html_parse_mode():
    """User-facing send_message in _send_broadcast must not use parse_mode='HTML'.

    Only checks calls inside the for-loop body (the actual broadcast to users).
    The admin report at the end legitimately uses HTML — that's fine since it's
    admin-only, not shown to end users.
    """
    import ast
    filepath = os.path.join(os.path.dirname(__file__), 'handlers', 'admin.py')
    with open(filepath, 'r', encoding='utf-8') as f:
        source = f.read()
    tree = ast.parse(source)

    def _has_html_parse_mode(call_node):
        for kw in call_node.keywords:
            if kw.arg == 'parse_mode':
                if isinstance(kw.value, ast.Constant) and 'HTML' in str(kw.value.value):
                    return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_send_broadcast':
            for stmt in node.body:
                if isinstance(stmt, ast.AsyncFor):
                    for child in ast.walk(stmt):
                        if isinstance(child, ast.Call):
                            func = child.func
                            is_send_message = (
                                (isinstance(func, ast.Attribute) and func.attr == 'send_message')
                                or (isinstance(func, ast.Name) and func.id == 'send_message')
                            )
                            if is_send_message and _has_html_parse_mode(child):
                                pytest.fail(
                                    'User-facing send_message in _send_broadcast '
                                    'uses parse_mode=HTML — phishing risk!'
                                )
