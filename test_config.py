"""
Tests for config validation, now_salon(), is_anamnesis_fresh(),
active booking statuses, and PROCEDURE_PRESETS.
"""

import sys
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from config import Config, SALON_TZ
from utils.helpers import (
    ANAMNESIS_FRESH_DAYS,
    ACTIVE_BOOKING_STATUSES,
    PROCEDURE_PRESETS,
    STATUS_CANCELLED,
    is_anamnesis_fresh,
    now_salon,
)


# ──────────────────────────────────────────────
# 1. Config.validate()
# ──────────────────────────────────────────────


class TestConfigValidate:
    def test_valid_config_returns_empty_errors(self):
        with patch.object(Config, "BOT_TOKEN", "123:ABC"), \
             patch.object(Config, "ADMIN_ID", 42):
            errors = Config.validate()
            assert errors == []

    def test_empty_bot_token_returns_error(self):
        with patch.object(Config, "BOT_TOKEN", ""), \
             patch.object(Config, "ADMIN_ID", 42):
            errors = Config.validate()
            assert any("BOT_TOKEN" in e for e in errors)

    def test_admin_id_zero_returns_error(self):
        with patch.object(Config, "BOT_TOKEN", "123:ABC"), \
             patch.object(Config, "ADMIN_ID", 0):
            errors = Config.validate()
            assert any("ADMIN_ID" in e for e in errors)


# ──────────────────────────────────────────────
# 2. Config defaults
# ──────────────────────────────────────────────


class TestConfigDefaults:
    def test_bonus_percent(self):
        assert Config.BONUS_PERCENT == 5

    def test_bonus_max_discount_percent(self):
        assert Config.BONUS_MAX_DISCOUNT_PERCENT == 50

    def test_booking_min_lead_minutes(self):
        assert Config.BOOKING_MIN_LEAD_MINUTES == 15

    def test_cancel_deadline_hours(self):
        assert Config.CANCEL_DEADLINE_HOURS == 2

    def test_anamnesis_fresh_days(self):
        assert ANAMNESIS_FRESH_DAYS == 7


# ──────────────────────────────────────────────
# 3. now_salon()
# ──────────────────────────────────────────────


class TestNowSalon:
    def test_returns_utc_plus_4_offset(self):
        """now_salon() should produce a datetime whose *source* is UTC+4."""
        # We can't check tzinfo on the result (it's stripped), but we can
        # compare it against datetime.now(UTC+4) — they should be within 1s.
        utc_plus_4 = timezone(timedelta(hours=4))
        expected = datetime.now(utc_plus_4).replace(tzinfo=None)
        result = now_salon()
        assert abs((result - expected).total_seconds()) < 1

    def test_returns_naive_datetime(self):
        result = now_salon()
        assert result.tzinfo is None


# ──────────────────────────────────────────────
# 4. is_anamnesis_fresh()
# ──────────────────────────────────────────────


class TestIsAnamnesisFresh:
    def _make_user(self, anamnesis_json=None, anamnesis_updated_at=None):
        return SimpleNamespace(
            anamnesis_json=anamnesis_json,
            anamnesis_updated_at=anamnesis_updated_at,
        )

    def test_fresh_when_updated_now(self):
        user = self._make_user(
            anamnesis_json='{"q": true}',
            anamnesis_updated_at=now_salon(),
        )
        assert is_anamnesis_fresh(user) is True

    def test_stale_when_updated_8_days_ago(self):
        user = self._make_user(
            anamnesis_json='{"q": true}',
            anamnesis_updated_at=now_salon() - timedelta(days=8),
        )
        assert is_anamnesis_fresh(user) is False

    def test_false_when_none_updated_at(self):
        user = self._make_user(
            anamnesis_json='{"q": true}',
            anamnesis_updated_at=None,
        )
        assert is_anamnesis_fresh(user) is False


# ──────────────────────────────────────────────
# 5. Active booking statuses
# ──────────────────────────────────────────────


class TestBookingStatuses:
    def test_active_statuses_contain_pending_and_confirmed(self):
        assert "pending" in ACTIVE_BOOKING_STATUSES
        assert "confirmed" in ACTIVE_BOOKING_STATUSES

    def test_status_cancelled_value(self):
        assert STATUS_CANCELLED == "cancelled"


# ──────────────────────────────────────────────
# 6. PROCEDURE_PRESETS
# ──────────────────────────────────────────────


class TestProcedurePresets:
    def test_consult(self):
        assert PROCEDURE_PRESETS["consult"] == "Консультация косметолога"

    def test_lips(self):
        assert PROCEDURE_PRESETS["lips"] == "Увеличение губ (филлер)"

    def test_botox(self):
        assert PROCEDURE_PRESETS["botox"] == "Ботулинотерапия (Ботокс)"
