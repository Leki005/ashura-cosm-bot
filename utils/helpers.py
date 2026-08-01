"""
Вспомогательные функции:
- Throttling (защита от спама)
- Валидаторы
- Форматтеры
- Работа с анамнезом
"""

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot
from aiogram.types import Message, CallbackQuery
from sqlalchemy import func, inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config, SALON_TZ


def now_salon() -> datetime:
    """Текущее время в часовом поясе салона (naive, для сравнения с БД)."""
    return datetime.now(SALON_TZ).replace(tzinfo=None)
from database import Booking, BonusTransaction, Review, Service, User
from utils.text_format import split_message, wrap_lines

logger = logging.getLogger(__name__)


def parse_callback_int(data: str | None, prefix: str) -> Optional[int]:
    """Извлекает int из callback data после удаления префикса. None при ошибке."""
    if not data:
        return None
    try:
        return int(data.removeprefix(prefix))
    except (ValueError, TypeError):
        return None


# Анамнез действителен 7 дней — не спрашиваем повторно
ANAMNESIS_FRESH_DAYS = 7

# Пресеты процедур при записи (ключ -> название в БД или для отображения)
PROCEDURE_PRESETS: dict[str, str] = {
    # --- Ботулинотерапия ---
    "botox": "Ботулинотерапия (Ботокс)",
    "botox_forehead": "Ботокс — лоб",
    "botox_glabella": "Ботокс — межбровка",
    "botox_crows": "Ботокс — гусиные лапки",
    "botox_fullface": "Ботокс — полное лицо",
    "botox_hyperhidrosis": "Ботокс — гипергидроз (подмышки)",
    # --- Филлеры ---
    "lips": "Увеличение губ (филлер)",
    "lipolytics": "Липолитики",
    # --- Аппаратные процедуры ---
    "morpheus8": "Morpheus8 (фракционный RF-лифтинг)",
    "bbl": "BBL (BroadBand Light — фотоомоложение)",
    # --- Лазер ---
    "laser_hair_removal": "Лазерная депиляция",
    "laser_face": "Лазерная депиляция — лицо",
    "laser_body": "Лазерная депиляция — тело",
    # --- Другое ---
    "consult": "Консультация косметолога",
}

# =============================================================================
# THROTTLING (защита от спама)
# =============================================================================

# Хранилище последних сообщений: {user_id: monotonic_timestamp}
_last_message_time: dict[int, float] = {}
_THROTTLE_CLEANUP_INTERVAL = 300  # Очистка каждые 5 минут
_THROTTLE_MAX_ENTRIES = 10000  # Максимум пользователей в памяти
_last_throttle_cleanup: float = 0


async def check_throttle(user_id: int) -> bool:
    """
    Проверяет, не слишком ли часто пользователь отправляет сообщения.
    Возвращает True если нужно игнорировать (throttle).
    """
    global _last_throttle_cleanup
    import time as _time
    now_ts = _time.monotonic()  # Монотонное время — без tz-проблем

    # Периодическая очистка старых записей
    if now_ts - _last_throttle_cleanup > _THROTTLE_CLEANUP_INTERVAL:
        cutoff = now_ts - 60
        stale = [uid for uid, ts in _last_message_time.items() if ts < cutoff]
        for uid in stale:
            del _last_message_time[uid]
        _last_throttle_cleanup = now_ts

    # Защита от переполнения: удаляем самых старых
    if len(_last_message_time) >= _THROTTLE_MAX_ENTRIES:
        sorted_items = sorted(_last_message_time.items(), key=lambda x: x[1])
        for uid, _ in sorted_items[:len(sorted_items) // 2]:
            del _last_message_time[uid]

    last_time = _last_message_time.get(user_id)

    if last_time is not None and (now_ts - last_time) < Config.THROTTLE_RATE:
        return True  # Слишком быстро — игнорируем

    _last_message_time[user_id] = now_ts
    return False


async def throttled_message(message: Message) -> None:
    """Отправляет предупреждение при спаме."""
    await message.answer(
        "⏳ Пожалуйста, не спешите! Подождите секунду перед следующим действием."
    )


# =============================================================================
# ВАЛИДАТОРЫ
# =============================================================================


def validate_phone(phone: str) -> Optional[str]:
    """
    Нормализует и валидирует номер телефона.
    Принимает российские и международные номера.
    Возвращает нормализованный номер или None если невалидный.
    """
    # Убираем всё кроме цифр и +
    cleaned = phone.strip()
    digits = "".join(c for c in cleaned if c.isdigit())

    # Российский номер: 11 цифр начиная с 7 или 8
    if len(digits) == 11 and digits[0] in "78":
        return "7" + digits[1:]  # Нормализуем к формату 79XXXXXXXXX
    if len(digits) == 10 and digits[0] == "9":
        return "7" + digits

    # Международный номер: начинается с + или 00, 10-15 цифр
    if cleaned.startswith("+") and 10 <= len(digits) <= 15:
        return "+" + digits
    if cleaned.startswith("00") and 10 <= len(digits) <= 15:
        return "+" + digits

    # Международный номер без + (но с кодом страны, 10-15 цифр)
    if 10 <= len(digits) <= 15 and not digits.startswith("7"):
        return "+" + digits

    return None


def validate_name(name: str) -> bool:
    """Проверяет, что имя валидно (не пустое, не слишком длинное)."""
    return 1 <= len(name.strip()) <= 50


# Популярные слоты времени для записи
BOOKING_TIME_SLOTS: tuple[str, ...] = (
    "10:00", "11:00", "12:00",
    "13:00", "14:00", "15:00",
    "16:00", "17:00", "18:00",
)


def parse_normalized_booking_date(date_str: str):
    """Парсит ДД.ММ.ГГГГ в date или None."""
    from datetime import date as date_cls, datetime as dt

    try:
        return dt.strptime(date_str.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def booking_slot_datetime(date_str: str, time_str: str):
    """Объединяет дату и время записи в naive datetime (локальное)."""
    from datetime import datetime as dt, time as time_cls

    day = parse_normalized_booking_date(date_str)
    if not day:
        return None
    try:
        hour, minute = map(int, time_str.strip().split(":", 1))
        return dt.combine(day, time_cls(hour, minute))
    except (ValueError, AttributeError):
        return None


def get_available_time_slots_for_date(date_str: str) -> list[str]:
    """
    Слоты времени для выбранной даты.
    На сегодня — только те, до которых ≥ BOOKING_MIN_LEAD_MINUTES.
    """
    from datetime import datetime as dt, timedelta

    day = parse_normalized_booking_date(date_str)
    if not day:
        return []
    today = now_salon().date()
    if day < today:
        return []
    if day > today:
        return list(BOOKING_TIME_SLOTS)

    cutoff = now_salon() + timedelta(minutes=Config.BOOKING_MIN_LEAD_MINUTES)
    slots: list[str] = []
    for slot in BOOKING_TIME_SLOTS:
        slot_dt = booking_slot_datetime(date_str, slot)
        if slot_dt and slot_dt >= cutoff:
            slots.append(slot)
    return slots


def get_booking_date_choices(max_days: int = 7) -> list:
    """Даты для кнопок: без прошлого; сегодня — только если есть слоты."""
    from datetime import datetime as dt, timedelta

    today = now_salon().date()
    choices = []
    for offset in range(max_days):
        day = today + timedelta(days=offset)
        date_str = day.strftime("%d.%m.%Y")
        if offset == 0 and not get_available_time_slots_for_date(date_str):
            continue
        choices.append(day)
    return choices


def check_booking_date_allowed(date_str: str) -> tuple[bool, str]:
    """Проверяет, можно ли записаться на эту дату."""
    day = parse_normalized_booking_date(date_str)
    if not day:
        return False, "Не удалось распознать дату."
    today = now_salon().date()
    if day < today:
        return False, "Нельзя записаться на прошедшую дату. Выберите сегодня или позже."
    if day == today and not get_available_time_slots_for_date(date_str):
        return (
            False,
            f"На сегодня запись недоступна — до ближайшего приёма меньше "
            f"{Config.BOOKING_MIN_LEAD_MINUTES} минут. Выберите другой день.",
        )
    return True, ""


def check_booking_time_allowed(date_str: str, time_str: str) -> tuple[bool, str]:
    """Проверяет, можно ли записаться на это время."""
    from datetime import timedelta

    ok, err = check_booking_date_allowed(date_str)
    if not ok:
        return False, err

    slot_dt = booking_slot_datetime(date_str, time_str)
    if not slot_dt:
        return False, "Не удалось распознать время."

    cutoff = now_salon() + timedelta(minutes=Config.BOOKING_MIN_LEAD_MINUTES)
    if slot_dt < cutoff:
        return (
            False,
            f"До начала приёма должно оставаться не меньше "
            f"{Config.BOOKING_MIN_LEAD_MINUTES} минут. Выберите другое время.",
        )
    return True, ""


def validate_booking_date(text: str) -> Optional[str]:
    """
    Проверяет и нормализует дату записи.
    Принимает: ДД.ММ.ГГГГ, ДД.ММ, ДД/ММ/ГГГГ.
    Возвращает строку ДД.ММ.ГГГГ или None (в т.ч. для прошедших дат).
    """
    import re
    from datetime import datetime

    raw = text.strip().replace("/", ".").replace("-", ".")
    today = now_salon().date()
    current_year = now_salon().year
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if parsed.date() < today:
                return None
            normalized = parsed.strftime("%d.%m.%Y")
            ok, _ = check_booking_date_allowed(normalized)
            return normalized if ok else None
        except ValueError:
            continue

    # Короткий формат ДД.ММ — без strptime (DeprecationWarning в Python 3.14+)
    parts = raw.split(".")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        try:
            day, month = int(parts[0]), int(parts[1])
            parsed = datetime(current_year, month, day)
            if parsed.date() < today:
                parsed = datetime(current_year + 1, month, day)
            normalized = parsed.strftime("%d.%m.%Y")
            ok, _ = check_booking_date_allowed(normalized)
            return normalized if ok else None
        except (ValueError, OverflowError):
            pass

    if re.fullmatch(r"\d{8}", raw):
        try:
            parsed = datetime.strptime(raw, "%d%m%Y")
            if parsed.date() < today:
                return None
            normalized = parsed.strftime("%d.%m.%Y")
            ok, _ = check_booking_date_allowed(normalized)
            return normalized if ok else None
        except ValueError:
            pass
    return None


def validate_booking_time(text: str) -> Optional[str]:
    """
    Проверяет и нормализует время записи.
    Принимает: 14:00, 9:30, 14.00.
    Возвращает строку ЧЧ:ММ или None.
    """
    from datetime import datetime

    raw = text.strip().replace(".", ":").replace(" ", "")
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
            hour, minute = parsed.hour, parsed.minute
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        except ValueError:
            continue
    return None


def format_date_from_callback(yyyymmdd: str) -> str:
    """Преобразует 20260715 → 15.07.2026."""
    from datetime import datetime

    try:
        parsed = datetime.strptime(yyyymmdd, "%Y%m%d")
        return parsed.strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return yyyymmdd or ""


def format_time_from_callback(hhmm: str) -> str:
    """Преобразует 1430 → 14:30."""
    if not hhmm or len(hhmm) < 2:
        return hhmm or ""
    return f"{hhmm[:2]}:{hhmm[2:4]}"


# =============================================================================
# ФОРМАТТЕРЫ
# =============================================================================


def format_phone(phone: str) -> str:
    """Форматирует телефон для красивого отображения."""
    if not phone:
        return "—"
    # Российский номер
    if len(phone) == 11 and phone.startswith("7"):
        return f"+7 ({phone[1:4]}) {phone[4:7]}-{phone[7:9]}-{phone[9:11]}"
    # Международный номер с +
    if phone.startswith("+"):
        return phone
    # Прочее — добавляем +
    return f"+{phone}"


def format_datetime(dt: Optional[datetime]) -> str:
    """Форматирует дату/время для отображения."""
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M")


def format_price(price: int) -> str:
    """Форматирует цену: 10 000 ₽"""
    return f"{price:,} ₽".replace(",", " ")


# =============================================================================
# АНАМНЕЗ
# =============================================================================


def is_anamnesis_fresh(user: User) -> bool:
    """Анамнез заполнен менее 7 дней назад — можно не спрашивать снова."""
    if not user.anamnesis_json or not user.anamnesis_updated_at:
        return False
    age = now_salon() - user.anamnesis_updated_at
    return age < timedelta(days=ANAMNESIS_FRESH_DAYS)


async def save_user_anamnesis(
    session: AsyncSession, user: User, anamnesis_json: str,
) -> None:
    """Сохраняет анамнез в профиль клиента на 7 дней."""
    user.anamnesis_json = anamnesis_json
    user.anamnesis_updated_at = now_salon()
    await session.flush()


async def resolve_procedure_service(
    session: AsyncSession, procedure_key: str, custom_name: Optional[str] = None,
) -> tuple[Optional[int], str]:
    """
    Находит услугу в каталоге по пресету или возвращает только название.
    Возвращает (service_id | None, отображаемое имя).
    Для 'other' — fuzzy-поиск по частичному совпадению.
    """
    if procedure_key == "other":
        name = (custom_name or "Другая процедура").strip()
        # Сначала точное совпадение
        result = await session.execute(
            select(Service).where(Service.name == name, Service.is_active == True)
        )
        svc = result.scalar_one_or_none()
        if svc:
            return svc.id, svc.name

        # Fuzzy: улучшенный поиск (подстрока → все слова → символьное пересечение)
        result = await session.execute(
            select(Service).where(Service.is_active == True)
        )
        name_lower = name.lower()
        name_words = [w for w in name_lower.split() if len(w) >= 2]
        all_services = list(result.scalars().all())

        best_match = None
        best_score = 0  # higher = better

        for s in all_services:
            svc_lower = s.name.lower()
            # 1) Exact substring match (search term inside service name) — best
            if name_lower in svc_lower:
                score = 100 + len(name_lower)
                if score > best_score:
                    best_score = score
                    best_match = s
                continue
            # 2) Reverse: service name inside search term
            if svc_lower in name_lower:
                score = 80 + len(svc_lower)
                if score > best_score:
                    best_score = score
                    best_match = s
                continue
            # 3) All search words appear in service name
            if name_words and all(w in svc_lower for w in name_words):
                score = 60 + sum(len(w) for w in name_words)
                if score > best_score:
                    best_score = score
                    best_match = s
                continue

        if best_match:
            return best_match.id, best_match.name

        # Не нашли — возвращаем как кастомную процедуру (service_id=None, это ОК)
        return None, name

    display_name = PROCEDURE_PRESETS.get(procedure_key, custom_name or "Процедура")
    result = await session.execute(
        select(Service).where(Service.name == display_name, Service.is_active == True)
    )
    svc = result.scalar_one_or_none()
    if svc:
        return svc.id, svc.name
    # Частичный поиск (например «Ботокс» в длинном названии)
    result = await session.execute(
        select(Service).where(Service.is_active == True)
    )
    for svc in result.scalars().all():
        if display_name.lower() in svc.name.lower():
            return svc.id, svc.name
    return None, display_name


def build_anamnesis_message(question_index: int, answers: dict) -> str:
    """
    Текст анкеты в одном сообщении — вопросы в теле, не в кнопках.
    Удобно читать на мобильном.
    """
    from keyboards import ANAMNESIS_QUESTIONS

    total = len(ANAMNESIS_QUESTIONS)
    lines = [f"📋 Анкета: вопрос {min(question_index + 1, total)} из {total}\n"]

    for i in range(question_index):
        q_text, q_key = ANAMNESIS_QUESTIONS[i]
        icon = "❌" if answers.get(q_key) else "✅"
        lines.append(wrap_lines(f"{icon} {i + 1}. {q_text}", width=40))

    if question_index < total:
        q_text, _ = ANAMNESIS_QUESTIONS[question_index]
        lines.append("")
        lines.append(wrap_lines(f"▶️ {q_text}", width=40))
        lines.append("\nНажмите ✅ Нет или ❌ Да")

    return "\n".join(lines)


def format_anamnesis(anamnesis_json: Optional[str]) -> str:
    """
    Форматирует JSON анамнеза в красивый текст с ✅/❌.
    Возвращает пустую строку если анамнеза нет.
    """
    if not anamnesis_json:
        return "📋 Анамнез: не заполнен"

    import json

    try:
        data = json.loads(anamnesis_json)
        if not isinstance(data, dict):
            return "📋 Анамнез: ошибка данных"
    except (json.JSONDecodeError, TypeError):
        return "📋 Анамнез: ошибка данных"

    lines = ["📋 Анамнез:"]
    for question, answer in data.items():
        icon = "❌" if answer else "✅"
        lines.append(wrap_lines(f"{icon} {question}", width=40))

    return "\n".join(lines)


def format_skin_anamnesis(skin_json: Optional[str]) -> str:
    """Форматирует анамнез кожи для уведомления админа."""
    if not skin_json:
        return ""

    import json

    try:
        data = json.loads(skin_json)
        if not isinstance(data, dict):
            return ""
    except (json.JSONDecodeError, TypeError):
        return ""

    type_names = {
        "oily": "Жирная", "dry": "Сухая", "combo": "Комбинированная",
        "normal": "Нормальная", "sensitive": "Чувствительная",
    }
    condition_names = {
        "better": "Лучше обычного", "same": "Как всегда",
        "worse": "Хуже обычного", "crisis": "Катастрофа",
    }
    problem_names = {
        "acne": "Акне", "comedones": "Чёрные точки", "postacne": "Постакне",
        "redness": "Покраснения", "sagging": "Потеря упругости", "wrinkles": "Морщины",
        "dehydration": "Обезвоживание", "dullness": "Тусклый цвет", "dark_circles": "Круги под глазами",
        "pigmentation": "Пигментация", "oiliness": "Жирный блеск", "sensitivity": "Чувствительность",
        "none": "Нет проблем",
    }
    dur_names = {
        "month": "Менее месяца", "3months": "1-3 месяца",
        "year": "3-12 месяцев", "over_year": "Больше года", "always": "Всегда",
    }
    infl_names = {"yes": "Да, активное", "no": "Нет", "was": "Было недавно"}
    budget_names = {
        "economy": "Эконом", "medium": "Средний",
        "premium": "Премиум", "unsure": "Не знает",
    }

    def _tr(names, val):
        return names.get(val, val) if val else "—"
    def _tr_list(names, vals):
        return ", ".join(names.get(v, v) for v in vals) if vals else "—"

    lines = [
        "✨ <b>АНАМНЕЗ КОЖИ (подбор ухода с ИИ):</b>",
        f"• Тип кожи: {_tr(type_names, data.get('skin_type'))}",
        f"• Состояние: {_tr(condition_names, data.get('skin_condition'))}",
        f"• Проблемы: {_tr_list(problem_names, data.get('problems', []))}",
        f"• Зоны: {', '.join(data.get('problem_areas', [])) or '—'}",
        f"• Давность: {_tr(dur_names, data.get('duration'))}",
        f"• Воспаление: {_tr(infl_names, data.get('inflammation'))}",
        f"• Аллергии: {data.get('allergies', '—')}",
        f"• Бюджет: {_tr(budget_names, data.get('budget'))}",
    ]
    if data.get("comment"):
        lines.append(f"• Комментарий: {html.escape(data['comment'])}")
    if data.get("ai_skin_analysis"):
        lines.append(f"\n📸 AI-анализ фото:\n{html.escape(data['ai_skin_analysis'][:500])}")

    return "\n".join(lines)


# =============================================================================
# БОНУСЫ
# =============================================================================


async def calculate_max_bonus_discount(
    session: AsyncSession, telegram_id: int, service_price: int
) -> int:
    """
    Вычисляет максимальную скидку бонусами для пользователя.
    Учитывает баланс и лимит 50% от суммы.
    """
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        return 0

    max_by_percent = service_price * Config.BONUS_MAX_DISCOUNT_PERCENT // 100
    return min(user.bonus_balance, max_by_percent)


async def add_bonus_transaction(
    session: AsyncSession,
    user_id: int,
    amount: int,
    description: str,
    booking_id: Optional[int] = None,
    tx_type: str = "MANUAL",
) -> None:
    """
    Создаёт запись о бонусной транзакции (начисление или списание).
    amount > 0 — начисление, amount < 0 — списание.
    """
    tx = BonusTransaction(
        user_id=user_id,
        amount=amount,
        tx_type=tx_type,
        booking_id=booking_id,
        description=description,
    )
    session.add(tx)
    await session.flush()


def calculate_booking_price(booking: Booking) -> int:
    """Сумма услуг записи: основная + дополнительные (по каталогу)."""
    total = 0
    if "service" not in sa_inspect(booking).unloaded and booking.service:
        total += booking.service.price or 0
    for item in parse_extra_services(booking):
        total += int(item.get("price") or 0)
    return total


def confirmation_bonus_amount(price: int) -> int:
    """3% бонусов (скидка) при подтверждении записи."""
    if price <= 0:
        return 0
    return price * Config.BONUS_PERCENT // 100


async def _booking_has_confirmation_bonus(
    session: AsyncSession, booking_id: int,
) -> bool:
    """Проверяет, начислялись ли уже бонусы при подтверждении этой записи."""
    result = await session.execute(
        select(BonusTransaction.id)
        .where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.tx_type == BonusTransaction.TX_CONFIRM_BONUS,
            BonusTransaction.amount > 0,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def grant_confirmation_bonus(
    session: AsyncSession, booking: Booking,
) -> int:
    """
    Начисляет бонусы за лояльность при подтверждении записи.
    Скидка зависит от количества завершённых визитов:
      0 визитов (новый клиент) → 0%
      1-2 визита → 3%
      3+ визитов → 5%
    Возвращает сумму начисления или 0.
    """
    if await _booking_has_confirmation_bonus(session, booking.id):
        return 0

    # Считаем завершённые визиты клиента (кроме текущей записи)
    completed_count = await _count_completed_visits(session, booking.user_id, exclude_booking_id=booking.id)

    # Определяем процент скидки за лояльность
    discount_pct = get_loyalty_discount_percent(completed_count)
    if discount_pct <= 0:
        return 0

    price = calculate_booking_price(booking)
    amount = price * discount_pct // 100
    if amount <= 0:
        return 0

    # Атомарное начисление — защита от race condition
    from sqlalchemy import update as sa_update
    result = await session.execute(
        sa_update(User)
        .where(User.id == booking.user_id)
        .values(bonus_balance=User.bonus_balance + amount)
    )
    if result.rowcount == 0:
        logger.warning("grant_confirmation_bonus: user %s not found", booking.user_id)
        return 0
    await session.refresh(booking.user)
    await add_bonus_transaction(
        session,
        booking.user_id,
        amount,
        f"Бонус {discount_pct}% за лояльность ({completed_count} визитов) при подтверждении записи #{booking.id}",
        booking_id=booking.id,
        tx_type=BonusTransaction.TX_CONFIRM_BONUS,
    )
    await session.flush()
    logger.info(
        "Подтверждение #%s: +%s бонусов (%d%% за %d визитов) клиенту %s",
        booking.id, amount, discount_pct, completed_count, booking.user_id,
    )
    return amount


async def _count_completed_visits(
    session: AsyncSession, user_id: int, exclude_booking_id: int = 0,
) -> int:
    """Считает количество завершённых визитов клиента."""
    from sqlalchemy import func as sa_func
    result = await session.execute(
        select(sa_func.count(Booking.id))
        .where(
            Booking.user_id == user_id,
            Booking.status == "completed",
            Booking.id != exclude_booking_id,
        )
    )
    return result.scalar() or 0


def get_loyalty_discount_percent(completed_visits: int) -> int:
    """
    Возвращает процент скидки за лояльность по количеству завершённых визитов.
    """
    discount = 0
    for min_visits, pct in Config.LOYALTY_DISCOUNT_TIERS:
        if completed_visits >= min_visits:
            discount = pct
    return discount


# =============================================================================
# СТАТУСЫ ЗАПИСЕЙ
# =============================================================================

# Активные статусы — блокируют новую запись
ACTIVE_BOOKING_STATUSES: tuple[str, ...] = ("pending", "confirmed")
STATUS_CANCELLED = "cancelled"

BOOKING_STATUS_LABELS: dict[str, str] = {
    "pending": "⏳ Ожидает подтверждения",
    "confirmed": "✅ Подтверждена",
    "completed": "🏁 Выполнена",
    "cancelled": "❌ Отменена",
}


async def get_user_by_telegram_id(
    session: AsyncSession, telegram_id: int,
) -> User | None:
    """Находит пользователя по Telegram ID."""
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def get_active_booking(
    session: AsyncSession, user_id: int,
) -> Booking | None:
    """
    Возвращает последнюю активную запись (pending/confirmed).
    Отменённые (cancelled) и завершённые не учитываются.
    """
    from sqlalchemy.orm import joinedload
    result = await session.execute(
        select(Booking)
        .options(joinedload(Booking.service))
        .where(
            Booking.user_id == user_id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        )
        .order_by(Booking.created_at.desc())
        .limit(1)
    )
    return result.unique().scalar_one_or_none()


async def cancel_booking_by_id(
    session: AsyncSession, booking_id: int, user_id: int,
) -> Booking | None:
    """
    Отменяет запись — атомарный UPDATE (защита от double-cancel).
    Возвращает обновлённую запись или None если не найдена/не ваша.
    """
    from sqlalchemy import update as sa_update
    result = await session.execute(
        sa_update(Booking)
        .where(
            Booking.id == booking_id,
            Booking.user_id == user_id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        )
        .values(status=STATUS_CANCELLED)
    )
    if result.rowcount == 0:
        return None
    # Загружаем обновлённую запись
    booking_result = await session.execute(
        select(Booking).where(Booking.id == booking_id)
    )
    booking = booking_result.scalar_one_or_none()
    if booking:
        logger.info("Запись #%s отменена (user_id=%s)", booking_id, user_id)
    return booking


def format_booking_status(status: str) -> str:
    """Человекочитаемый статус записи."""
    return BOOKING_STATUS_LABELS.get(status, status)


def parse_extra_services(booking: Booking) -> list[dict]:
    """Список доп. услуг из JSON поля extra_services_json."""
    if not booking.extra_services_json:
        return []
    import json

    try:
        data = json.loads(booking.extra_services_json)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]
    except (json.JSONDecodeError, TypeError):
        return []


def format_booking_services_line(booking: Booking) -> str:
    """Все процедуры записи: основная + объединённые. Без lazy-load relationship."""
    names: list[str] = []
    # В async SQLAlchemy доступ к неподгруженному relationship → greenlet_spawn error
    if "service" not in sa_inspect(booking).unloaded and booking.service:
        names.append(booking.service.name)
    for item in parse_extra_services(booking):
        name = item.get("name")
        if name and name not in names:
            names.append(name)
    if not names:
        return "Запись по заявке"
    return " + ".join(names)


def format_active_booking_prompt(
    booking: Booking, new_service_name: Optional[str] = None,
) -> str:
    """Текст выбора: совместить с текущей записью или записаться на другое время."""
    card = format_booking_card(booking)
    lines = [
        "У вас уже есть активная запись:\n",
        card,
        f"\n💅 Текущие процедуры: {format_booking_services_line(booking)}",
    ]
    if new_service_name:
        lines.append(f"\n➕ Новая услуга: {new_service_name}")
    lines.append("\nЧто сделать?")
    return "\n".join(lines)


async def merge_service_into_booking(
    session: AsyncSession, booking: Booking, service: Service,
) -> bool:
    """
    Добавляет услугу к существующей записи.
    Возвращает False если услуга уже есть в записи.
    """
    import json

    existing_ids: set[int] = set()
    if booking.service_id:
        existing_ids.add(booking.service_id)
    for item in parse_extra_services(booking):
        if item.get("id"):
            existing_ids.add(int(item["id"]))

    if service.id in existing_ids:
        return False

    # Если основной услуги ещё нет — назначаем её основной
    if not booking.service_id:
        booking.service_id = service.id
    else:
        extras = parse_extra_services(booking)
        extras.append({
            "id": service.id,
            "name": service.name,
            "price": service.price,
        })
        booking.extra_services_json = json.dumps(extras, ensure_ascii=False)

    note_line = f"+ {service.name}"
    booking.notes = f"{booking.notes}\n{note_line}" if booking.notes else note_line
    await session.flush()
    logger.info(
        "Услуга «%s» добавлена к записи #%s",
        service.name, booking.id,
    )
    return True


def format_booking_card(booking: Booking) -> str:
    """Карточка записи для раздела «Мои записи» — понятные статусы и дата."""
    service_name = format_booking_services_line(booking)

    status = format_booking_status(booking.status)

    # Дата/время: админ часто пишет всё в preferred_date
    if booking.preferred_date:
        when = booking.preferred_date
        if booking.preferred_time:
            when = f"{when}, {booking.preferred_time}"
    elif booking.status == "pending":
        when = (
            f"ожидает согласования "
            f"(заявка от {booking.created_at.strftime('%d.%m.%Y')})"
        )
    else:
        when = "не назначена"

    lines = [
        f"#{booking.id} — {service_name}",
        f"Статус: {status}",
        f"📅 Когда: {when}",
    ]
    if booking.notes:
        lines.append(wrap_lines(f"📝 {booking.notes}", width=40))
    return "\n".join(lines)


def format_service_detail(service: Service) -> list[str]:
    """
    Карточка услуги — одно или несколько сообщений.
    Без лишнего декора у цены.
    """
    parts = [f"💅 {service.name}"]

    if service.description:
        parts.append(wrap_lines(service.description, width=42))

    parts.append(f"Цена: {format_price(service.price)}")
    if service.duration:
        parts.append(f"Длительность: {service.duration} мин.")

    return split_message("\n\n".join(parts))


# =============================================================================
# УВЕДОМЛЕНИЯ
# =============================================================================


async def send_message_to_owner(
    bot: Bot, text: str, reply_markup=None, *, parse_mode: str | None = None,
) -> bool:
    """
    Отправляет сообщение владельцу (OWNER_ID, затем ADMIN_ID как запасной).
    Возвращает True если хотя бы одна доставка успешна.
    """
    sent = False
    for chat_id in {Config.owner_id(), Config.ADMIN_ID}:
        if not chat_id:
            continue
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            sent = True
            logger.info("Сообщение владельцу доставлено: chat_id=%s", chat_id)
        except Exception as e:
            logger.warning("Не удалось отправить владельцу %s: %s", chat_id, e)
    return sent


async def notify_admin_new_booking(
    bot: Bot,
    booking_id: int,
    user: User,
    service: Optional[Service],
    anamnesis_json: Optional[str],
    notes: Optional[str],
    bonus_used: int = 0,
    *,
    telegram_id: Optional[int] = None,
    service_name: Optional[str] = None,
    username: Optional[str] = None,
    preferred_date: Optional[str] = None,
    preferred_time: Optional[str] = None,
) -> None:
    """
    Отправляет Ашуре уведомление о новой заявке.
    Ключевые поля (Telegram ID + услуга) — в первом коротком сообщении.
    """
    from keyboards import admin_booking_keyboard

    tg_id = telegram_id or user.telegram_id
    resolved_service = service_name or (service.name if service else "Запись по заявке")
    service_price = service.price if service else 0
    final_price = service_price - bonus_used if service else 0

    logger.info(
        "Уведомление о записи #%s: tg_id=%s service=%s",
        booking_id, tg_id, resolved_service,
    )

    when = "—"
    if preferred_date and preferred_time:
        when = f"{preferred_date}, {preferred_time}"
    elif preferred_date:
        when = preferred_date

    # Главное сообщение — ID, услуга и время в начале
    # Показываем анамнез только если он заполнен за последние 7 дней
    has_skin = False
    if user.skin_anamnesis_json and user.skin_anamnesis_at:
        from datetime import timedelta
        if now_salon() - user.skin_anamnesis_at < timedelta(days=7):
            has_skin = True
    core_text = (
        f"🆕 <b>НОВАЯ ЗАПИСЬ #{booking_id}</b>\n\n"
        f"🆔 Telegram ID: <code>{tg_id}</code>\n"
        f"💅 Услуга: <b>{html.escape(resolved_service)}</b>\n"
        f"📅 Когда: <b>{when}</b>\n\n"
        f"👤 Клиент: {html.escape(user.name)}\n"
        f"📱 Телефон: {format_phone(user.phone)}\n"
    )
    if has_skin:
        core_text += "✨ <b>Прошёл подбор ухода с ИИ</b>\n"
    if username:
        core_text += f"🔗 Telegram: @{html.escape(username)}\n"

    from utils.privacy import format_pd_consent_admin_line

    core_text += format_pd_consent_admin_line(user)

    if service:
        core_text += f"💰 Стоимость: {format_price(service.price)}\n"
        if bonus_used > 0:
            core_text += (
                f"🎁 Скидка бонусами: -{format_price(bonus_used)}\n"
                f"💰 Итого к оплате: {format_price(final_price)}\n"
            )

    delivered = await send_message_to_owner(
        bot,
        core_text,
        reply_markup=admin_booking_keyboard(booking_id),
        parse_mode="HTML",
    )

    # Детали — отдельным сообщением (не мешают доставке главного)
    details_parts = []
    if notes:
        details_parts.append(f"📝 Пожелания клиента:\n{html.escape(notes)}")
    if anamnesis_json:
        details_parts.append(format_anamnesis(anamnesis_json))
    if has_skin:
        details_parts.append(format_skin_anamnesis(user.skin_anamnesis_json))

    if details_parts and delivered:
        await send_message_to_owner(bot, "\n\n".join(details_parts))

    if not delivered:
        raise RuntimeError(
            "Не удалось уведомить владельца о записи. "
            "Админ должен нажать /start в боте."
        )


async def notify_admin_service_merged(
    bot: Bot,
    booking: Booking,
    user: User,
    *,
    telegram_id: int,
    username: Optional[str] = None,
    service: Optional[Service] = None,
    note_text: Optional[str] = None,
) -> None:
    """Уведомляет админа о добавлении услуги/процедур к существующей записи."""
    from keyboards import admin_booking_keyboard, admin_booking_confirmed_keyboard

    all_services = format_booking_services_line(booking)
    added_line = (
        f"💅 Новая услуга: {html.escape(service.name)}\n" if service
        else f"📝 Добавлено: {html.escape(note_text)}\n" if note_text
        else ""
    )
    text = (
        f"➕ УСЛУГА ДОБАВЛЕНА к записи #{booking.id}\n\n"
        f"🆔 Telegram ID: {telegram_id}\n"
        f"👤 Клиент: {html.escape(user.name)}\n"
        f"📱 Телефон: {format_phone(user.phone)}\n"
        f"{added_line}"
        f"📋 Все процедуры: {html.escape(all_services)}\n"
        f"📅 Дата: {booking.preferred_date or '—'}\n"
        f"⏰ Время: {booking.preferred_time or '—'}\n"
    )
    if username:
        text += f"🔗 Telegram: @{html.escape(username)}\n"

    # Клавиатура зависит от статуса записи
    if booking.status == "confirmed":
        kb = admin_booking_confirmed_keyboard(booking.id)
    else:
        kb = admin_booking_keyboard(booking.id)
    delivered = await send_message_to_owner(bot, text, reply_markup=kb)
    if not delivered:
        raise RuntimeError("Не удалось уведомить владельца об объединении записи.")


def format_contacts_text() -> str:
    """Текст раздела «Контакты» с полной информацией о салоне."""
    return (
        "📞 Контакты\n\n"
        f"📱 Телефон: {Config.SALON_PHONE}\n"
        f"📍 Адрес: {Config.SALON_ADDRESS}\n"
        f"📷 Instagram: {Config.INSTAGRAM_LINK}\n"
        f"💬 WhatsApp: по номеру {Config.WHATSAPP_NUMBER}\n"
        f"📲 Max: можно найти по номеру {Config.SALON_PHONE}\n\n"
        "Записывайтесь прямо здесь, в боте! 💫"
    )


async def notify_admin_procedure_feedback(
    bot: Bot,
    booking: Booking,
    user: User,
    feedback: str,
) -> None:
    """Пересылает Ашуре ответ клиента на опрос после процедуры."""
    service_name = format_booking_services_line(booking)
    text = (
        f"💬 <b>Как прошла процедура — запись #{booking.id}</b>\n\n"
        f"👤 {html.escape(user.name)}\n"
        f"📱 {format_phone(user.phone)}\n"
        f"🆔 Telegram ID: {user.telegram_id}\n"
        f"💅 {html.escape(service_name)}\n\n"
        f"📝 {html.escape(feedback)}"
    )
    await send_message_to_owner(bot, text)


async def notify_admin_cancel(
    bot: Bot, user: User, booking: Booking, reason: str,
) -> None:
    """Уведомляет Ашуру об отмене записи клиентом."""
    text = (
        f"❌ Запись отменена клиентом!\n\n"
        f"🆔 Telegram ID: <code>{user.telegram_id}</code>\n"
        f"👤 Клиент: {html.escape(user.name)}\n"
        f"📱 Телефон: {format_phone(user.phone)}\n"
        f"💅 Услуга: {format_booking_services_line(booking)}\n"
        f"📅 Была на: {booking.preferred_date or '—'} {booking.preferred_time or ''}\n"
        f"📝 Причина: {html.escape(reason)}"
    )

    await send_message_to_owner(bot, text, parse_mode="HTML")


async def notify_client_booking_cancelled(
    bot: Bot,
    telegram_id: int,
    *,
    service_name: str,
    reason: Optional[str] = None,
) -> None:
    """Уведомляет клиента об отмене записи админом."""
    text = (
        "❌ Ваша запись отменена администратором.\n\n"
        f"💅 Процедура: {service_name}\n"
    )
    if reason and reason not in ("-", "—"):
        text += f"📝 Причина: {reason}\n"
    text += (
        f"\nСвяжитесь с Ашурой: {format_phone(Config.SALON_PHONE)}\n"
        "или запишитесь заново через бот."
    )
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
    except Exception as e:
        logger.warning('Failed to notify client %s: %s', telegram_id, e)


async def notify_client_booking_confirmed(
    bot: Bot,
    telegram_id: int,
    booking: Booking,
    *,
    service_name: Optional[str] = None,
    bonus_granted: int = 0,
    discount_percent: int = 0,
) -> None:
    """Отправляет клиенту подтверждение записи."""
    # service_name передаём явно — без lazy-load relationship (greenlet_spawn)
    resolved = service_name or format_booking_services_line(booking)

    text = (
        f"✅ <b>Ваша запись подтверждена!</b>\n\n"
        f"💅 Услуга: {html.escape(resolved)}\n"
        f"📅 Дата: {booking.preferred_date or '—'}\n"
        f"⏰ Время: {booking.preferred_time or '—'}\n\n"
        f"📍 {Config.SALON_ADDRESS}\n"
        f"📱 {format_phone(Config.SALON_PHONE)}"
    )
    if bonus_granted > 0:
        text += (
            f"\n\n🎁 <b>Скидка за лояльность: {discount_percent}%</b>\n"
            f"Вам начислено +{bonus_granted} бонусов!\n"
            f"💰 Баланс: {booking.user.bonus_balance} бонусов"
        )
    text += "\n\nЖду вас! 💫"

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning('Failed to notify client %s: %s', telegram_id, e)


# =============================================================================
# СТАТИСТИКА
# =============================================================================


async def get_stats(
    session: AsyncSession, period: str = "all",
) -> dict:
    """
    Собирает статистику за указанный период.
    period: today, week, month, all
    """
    now = now_salon()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        start = datetime.min

    # Количество клиентов
    clients_result = await session.execute(
        select(func.count(User.id)).where(User.created_at >= start)
    )
    total_clients = clients_result.scalar() or 0

    # Всего клиентов
    all_clients_result = await session.execute(select(func.count(User.id)))
    all_clients = all_clients_result.scalar() or 0

    # Записи по статусам
    bookings_result = await session.execute(
        select(Booking.status, func.count(Booking.id))
        .where(Booking.created_at >= start)
        .group_by(Booking.status)
    )
    bookings_by_status = {status: count for status, count in bookings_result.all()}

    # Выручка (выполненные записи)
    revenue_result = await session.execute(
        select(func.sum(Booking.total_amount))
        .where(Booking.status == "completed")
        .where(Booking.completed_at >= start)
    )
    revenue = revenue_result.scalar() or 0

    # Отзывы
    reviews_result = await session.execute(
        select(func.count(Review.id), func.avg(Review.rating))
        .where(Review.is_published == True)
    )
    row = reviews_result.one_or_none()
    if row:
        review_count, avg_rating = row
    else:
        review_count, avg_rating = 0, 0
    review_count = review_count or 0
    avg_rating = round(avg_rating, 1) if avg_rating else 0

    # Общий бонусный баланс всех клиентов
    bonuses_result = await session.execute(select(func.sum(User.bonus_balance)))
    total_bonuses = bonuses_result.scalar() or 0

    return {
        "period": period,
        "new_clients": total_clients,
        "total_clients": all_clients,
        "bookings_pending": bookings_by_status.get("pending", 0),
        "bookings_confirmed": bookings_by_status.get("confirmed", 0),
        "bookings_completed": bookings_by_status.get("completed", 0),
        "bookings_cancelled": bookings_by_status.get("cancelled", 0),
        "revenue": revenue,
        "reviews_count": review_count,
        "avg_rating": avg_rating,
        "total_bonuses": total_bonuses,
    }


def format_stats(stats: dict) -> str:
    """Форматирует статистику в красивый текст."""
    period_names = {
        "today": "Сегодня",
        "week": "За неделю",
        "month": "За месяц",
        "all": "За всё время",
    }

    return (
        f"📊 Статистика: {period_names.get(stats['period'], stats['period'])}\n\n"
        f"👥 Клиенты:\n"
        f"  • Новых: {stats['new_clients']}\n"
        f"  • Всего: {stats['total_clients']}\n\n"
        f"📋 Записи:\n"
        f"  • 🆕 Новые: {stats['bookings_pending']}\n"
        f"  • ✅ Подтверждённые: {stats['bookings_confirmed']}\n"
        f"  • 🏁 Выполненные: {stats['bookings_completed']}\n"
        f"  • ❌ Отменённые: {stats['bookings_cancelled']}\n\n"
        f"💰 Выручка: {format_price(stats['revenue'])}\n\n"
        f"★ Отзывы: {stats['reviews_count']} (средний: {stats['avg_rating']})\n\n"
        f"🎁 Бонусов у клиентов: {stats['total_bonuses']}"
    )
