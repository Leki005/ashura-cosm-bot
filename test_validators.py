"""Unit tests for validators and formatters."""

import pytest
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from utils.helpers import (
    validate_phone,
    validate_name,
    validate_booking_date,
    validate_booking_time,
    format_phone,
    format_price,
    format_date_from_callback,
    format_time_from_callback,
    now_salon,
    is_anamnesis_fresh,
)
from utils.text_format import truncate_button, split_message, wrap_lines
from database import User


# =============================================================================
# validate_phone
# =============================================================================

class TestValidatePhone:
    def test_valid_8_prefix(self):
        assert validate_phone("89885919401") == "79885919401"

    def test_valid_7_prefix(self):
        assert validate_phone("79885919401") == "79885919401"

    def test_valid_9_prefix(self):
        assert validate_phone("9885919401") == "79885919401"

    def test_invalid_short(self):
        assert validate_phone("123") is None

    def test_invalid_empty(self):
        assert validate_phone("") is None

    def test_invalid_letters(self):
        assert validate_phone("abc") is None

    def test_with_plus_prefix(self):
        result = validate_phone("+79885919401")
        assert result == "79885919401"

    def test_with_spaces(self):
        result = validate_phone("8 988 591 94 01")
        assert result == "79885919401"

    def test_with_dashes(self):
        result = validate_phone("8-988-591-94-01")
        assert result == "79885919401"


# =============================================================================
# validate_name
# =============================================================================

class TestValidateName:
    def test_valid_name(self):
        assert validate_name("Ашура") is True

    def test_single_char(self):
        assert validate_name("A") is True

    def test_empty(self):
        assert validate_name("") is False

    def test_too_long(self):
        assert validate_name("x" * 51) is False

    def test_with_spaces(self):
        assert validate_name("  Ашура  ") is True

    def test_max_length(self):
        assert validate_name("x" * 50) is True


# =============================================================================
# validate_booking_date
# =============================================================================

class TestValidateBookingDate:
    def test_valid_format(self):
        result = validate_booking_date("15.07.2030")
        assert result is not None
        assert result == "15.07.2030"

    def test_invalid_format(self):
        assert validate_booking_date("abc") is None

    def test_past_date(self):
        assert validate_booking_date("01.01.2020") is None

    def test_short_format(self):
        result = validate_booking_date("15.07")
        assert result is not None

    def test_slash_format(self):
        result = validate_booking_date("15/07/2030")
        assert result is not None


# =============================================================================
# validate_booking_time
# =============================================================================

class TestValidateBookingTime:
    def test_valid_time(self):
        assert validate_booking_time("14:00") == "14:00"

    def test_valid_time_short(self):
        assert validate_booking_time("9:30") == "09:30"

    def test_dot_format(self):
        assert validate_booking_time("14.00") == "14:00"

    def test_invalid_hour(self):
        assert validate_booking_time("25:00") is None

    def test_invalid_format(self):
        assert validate_booking_time("abc") is None

    def test_midnight(self):
        assert validate_booking_time("00:00") == "00:00"

    def test_end_of_day(self):
        assert validate_booking_time("23:59") == "23:59"


# =============================================================================
# format_phone
# =============================================================================

class TestFormatPhone:
    def test_standard_format(self):
        assert format_phone("79885919401") == "+7 (988) 591-94-01"

    def test_passthrough_short(self):
        assert format_phone("123") == "123"


# =============================================================================
# format_price
# =============================================================================

class TestFormatPrice:
    def test_standard(self):
        assert format_price(10000) == "10 000 ₽"

    def test_zero(self):
        assert format_price(0) == "0 ₽"

    def test_small(self):
        assert format_price(100) == "100 ₽"


# =============================================================================
# format_date_from_callback / format_time_from_callback
# =============================================================================

class TestFormatFromCallback:
    def test_date(self):
        assert format_date_from_callback("20260715") == "15.07.2026"

    def test_time(self):
        assert format_time_from_callback("1430") == "14:30"


# =============================================================================
# truncate_button
# =============================================================================

class TestTruncateButton:
    def test_short_unchanged(self):
        assert truncate_button("Короткий текст") == "Короткий текст"

    def test_long_truncated(self):
        result = truncate_button("Очень длинный текст кнопки который не поместится на экран мобильного телефона", max_len=40)
        assert len(result) <= 40
        assert result.endswith("…")

    def test_exact_length(self):
        text = "x" * 40
        assert truncate_button(text, max_len=40) == text


# =============================================================================
# split_message
# =============================================================================

class TestSplitMessage:
    def test_short_unchanged(self):
        text = "Короткий текст"
        assert split_message(text) == [text]

    def test_long_split(self):
        text = ("A" * 2000 + "\n") * 3
        parts = split_message(text, max_len=3500)
        assert len(parts) >= 2
        assert all(len(p) <= 3500 for p in parts)


# =============================================================================
# wrap_lines
# =============================================================================

class TestWrapLines:
    def test_short_unchanged(self):
        assert wrap_lines("Короткий текст", width=42) == "Короткий текст"

    def test_long_wrapped(self):
        text = "Очень длинное предложение которое должно быть перенесено на следующую строку"
        result = wrap_lines(text, width=42)
        lines = result.split("\n")
        assert all(len(line) <= 42 for line in lines)

    def test_empty(self):
        assert wrap_lines("", width=42) == ""


# =============================================================================
# is_anamnesis_fresh
# =============================================================================

class TestIsAnamnesisFresh:
    def test_fresh(self):
        user = MagicMock()
        user.anamnesis_json = '{"test": true}'
        user.anamnesis_updated_at = now_salon()
        assert is_anamnesis_fresh(user) is True

    def test_stale(self):
        user = MagicMock()
        user.anamnesis_json = '{"test": true}'
        user.anamnesis_updated_at = now_salon() - timedelta(days=8)
        assert is_anamnesis_fresh(user) is False

    def test_none_json(self):
        user = MagicMock()
        user.anamnesis_json = None
        user.anamnesis_updated_at = now_salon()
        assert is_anamnesis_fresh(user) is False

    def test_none_updated_at(self):
        user = MagicMock()
        user.anamnesis_json = '{"test": true}'
        user.anamnesis_updated_at = None
        assert is_anamnesis_fresh(user) is False
