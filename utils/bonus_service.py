"""
Бонусный сервис — единый модуль для всех бонусных операций.
Заменяет размазанную логику из helpers.py, admin.py, client.py.
"""
import logging
from typing import Optional

from sqlalchemy import select, func as sa_func, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from database import BonusTransaction, Booking, User

logger = logging.getLogger(__name__)


# =============================================================================
# НАЧИСЛЕНИЕ
# =============================================================================

async def grant_loyalty_bonus(
    session: AsyncSession, booking: Booking,
) -> int:
    """
    Начисляет бонус за лояльность при подтверждении записи.
    Скидка зависит от количества завершённых визитов:
      0 визитов → 0%  |  1-2 → 3%  |  3+ → 5%
    Возвращает сумму начисления или 0.
    Идемпотентен: unique constraint на (booking_id, tx_type) защищает от двойного начисления.
    Savepoint: откатывает только бонус, не всю транзакцию (booking status сохраняется).
    """
    from sqlalchemy.exc import IntegrityError

    completed_count = await count_completed_visits(
        session, booking.user_id, exclude_booking_id=booking.id,
    )
    discount_pct = get_loyalty_discount_percent(completed_count)
    if discount_pct <= 0:
        return 0

    price = calculate_booking_price(booking)
    amount = price * discount_pct // 100
    if amount <= 0:
        return 0

    # Savepoint — если IntegrityError, откатится только бонус, не booking status
    try:
        async with session.begin_nested():
            result = await session.execute(
                sa_update(User)
                .where(User.id == booking.user_id)
                .values(bonus_balance=User.bonus_balance + amount)
            )
            if result.rowcount == 0:
                return 0
            await session.refresh(booking.user)
            await create_transaction(
                session, booking.user_id, amount,
                BonusTransaction.TX_CONFIRM_BONUS,
                f"Бонус {discount_pct}% за лояльность ({completed_count} визитов) при подтверждении записи #{booking.id}",
                booking_id=booking.id,
            )
    except IntegrityError:
        # Savepoint откатился — balance update отменён, booking status сохранён
        logger.info("grant_loyalty_bonus: already granted for booking %s", booking.id)
        return 0

    logger.info(
        "Подтверждение #%s: +%s бонусов (%d%% за %d визитов) клиенту %s",
        booking.id, amount, discount_pct, completed_count, booking.user_id,
    )
    return amount


async def grant_visit_bonus(
    session: AsyncSession, user_id: int, amount: int, booking_id: int,
) -> None:
    """Начисляет бонус за завершённый визит. Идемпотентен: unique constraint. Savepoint."""
    from sqlalchemy.exc import IntegrityError
    try:
        async with session.begin_nested():
            await _atomic_balance_update(session, user_id, amount)
            await create_transaction(
                session, user_id, amount,
                BonusTransaction.TX_VISIT_BONUS,
                f"Начисление за выполненную запись #{booking_id}",
                booking_id=booking_id,
            )
    except IntegrityError:
        logger.info("grant_visit_bonus: already granted for booking %s", booking_id)


async def grant_manual_bonus(
    session: AsyncSession, user_id: int, amount: int, description: str = "Ручное начисление администратором",
) -> None:
    """Ручное начисление бонусов админом."""
    await _atomic_balance_update(session, user_id, amount)
    await create_transaction(
        session, user_id, amount,
        BonusTransaction.TX_MANUAL,
        description,
    )


# =============================================================================
# СПИСАНИЕ
# =============================================================================

async def spend_bonuses(
    session: AsyncSession, user_id: int, amount: int, booking_id: int,
) -> bool:
    """
    Списывает бонусы при записи. Возвращает True если успешно.
    """
    result = await session.execute(
        sa_update(User)
        .where(User.id == user_id, User.bonus_balance >= amount)
        .values(bonus_balance=User.bonus_balance - amount)
    )
    if result.rowcount == 0:
        return False
    await create_transaction(
        session, user_id, -amount,
        BonusTransaction.TX_SPEND,
        f"Списание бонусов при записи #{booking_id}",
        booking_id=booking_id,
    )
    return True


# =============================================================================
# ВОЗВРАТ / ОТЗЫВ
# =============================================================================

async def refund_bonuses(
    session: AsyncSession, user_id: int, amount: int, booking_id: int,
) -> None:
    """Возвращает списанные бонусы при отмене записи."""
    await _atomic_balance_update(session, user_id, amount)
    await create_transaction(
        session, user_id, amount,
        BonusTransaction.TX_REFUND,
        f"Возврат бонусов при отмене записи #{booking_id}",
        booking_id=booking_id,
    )


async def revoke_confirmation_bonus(
    session: AsyncSession, user_id: int, booking_id: int,
) -> int:
    """
    Отзывает бонус за лояльность (CONFIRM_BONUS) при отмене/отклонении.
    Возвращает сумму отзыва. Идемпотентен: повторный вызов = 0.
    """
    # Проверяем — уже отозван CONFIRM_BONUS для этой записи?
    existing = await session.execute(
        select(BonusTransaction.id)
        .where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.tx_type == BonusTransaction.TX_REVOKE,
            BonusTransaction.description.contains("лояльность"),
            BonusTransaction.amount < 0,
        )
        .limit(1)
    )
    if existing.scalar_one_or_none():
        return 0

    tx = await find_transaction(
        session, booking_id,
        tx_type=BonusTransaction.TX_CONFIRM_BONUS,
        amount_sign="positive",
    )
    if not tx or tx.amount <= 0:
        return 0

    result = await session.execute(
        sa_update(User)
        .where(User.id == user_id)
        .values(bonus_balance=sa_func.max(0, User.bonus_balance - tx.amount))
    )
    await create_transaction(
        session, user_id, -tx.amount,
        BonusTransaction.TX_REVOKE,
        f"Отзыв бонуса лояльности при отмене записи #{booking_id}",
        booking_id=booking_id,
    )
    return tx.amount


async def revoke_visit_bonus(
    session: AsyncSession, user_id: int, booking_id: int,
) -> int:
    """
    Отзывает бонус за визит (VISIT_BONUS) при отклонении.
    Возвращает сумму отзыва. Идемпотентен: повторный вызов = 0.
    """
    # Проверяем — уже отозван VISIT_BONUS для этой записи?
    existing = await session.execute(
        select(BonusTransaction.id)
        .where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.tx_type == BonusTransaction.TX_REVOKE,
            BonusTransaction.description.contains("визит"),
            BonusTransaction.amount < 0,
        )
        .limit(1)
    )
    if existing.scalar_one_or_none():
        return 0

    tx = await find_transaction(
        session, booking_id,
        tx_type=BonusTransaction.TX_VISIT_BONUS,
        amount_sign="positive",
    )
    if not tx or tx.amount <= 0:
        return 0

    result = await session.execute(
        sa_update(User)
        .where(User.id == user_id)
        .values(bonus_balance=sa_func.max(0, User.bonus_balance - tx.amount))
    )
    await create_transaction(
        session, user_id, -tx.amount,
        BonusTransaction.TX_REVOKE,
        f"Отзыв бонуса визита при отмене записи #{booking_id}",
        booking_id=booking_id,
    )
    return tx.amount


# =============================================================================
# ПРОВЕРКИ
# =============================================================================

async def has_confirmation_bonus(
    session: AsyncSession, booking_id: int,
) -> bool:
    """Проверяет, начислялся ли бонус за лояльность для этой записи."""
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


async def has_visit_bonus(
    session: AsyncSession, booking_id: int,
) -> bool:
    """Проверяет, начислялся ли бонус за визит для этой записи."""
    result = await session.execute(
        select(BonusTransaction.id)
        .where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.tx_type == BonusTransaction.TX_VISIT_BONUS,
            BonusTransaction.amount > 0,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def find_transaction(
    session: AsyncSession, booking_id: int,
    tx_type: str, amount_sign: str = "positive",
) -> Optional[BonusTransaction]:
    """Находит транзакцию по booking_id и типу."""
    query = (
        select(BonusTransaction)
        .where(
            BonusTransaction.booking_id == booking_id,
            BonusTransaction.tx_type == tx_type,
        )
    )
    if amount_sign == "positive":
        query = query.where(BonusTransaction.amount > 0)
    elif amount_sign == "negative":
        query = query.where(BonusTransaction.amount < 0)
    result = await session.execute(query.order_by(BonusTransaction.id.desc()).limit(1))
    return result.scalar_one_or_none()


# =============================================================================
# УТИЛИТЫ
# =============================================================================

async def create_transaction(
    session: AsyncSession,
    user_id: int,
    amount: int,
    tx_type: str,
    description: str,
    booking_id: Optional[int] = None,
) -> BonusTransaction:
    """Создаёт бонусную транзакцию."""
    tx = BonusTransaction(
        user_id=user_id,
        amount=amount,
        tx_type=tx_type,
        booking_id=booking_id,
        description=description,
    )
    session.add(tx)
    return tx


async def _atomic_balance_update(
    session: AsyncSession, user_id: int, amount: int,
) -> None:
    """Атомарно обновляет баланс бонусов."""
    result = await session.execute(
        sa_update(User)
        .where(User.id == user_id)
        .values(bonus_balance=User.bonus_balance + amount)
    )
    if result.rowcount == 0:
        logger.warning("_atomic_balance_update: user %s not found", user_id)
        return
    await session.flush()


async def count_completed_visits(
    session: AsyncSession, user_id: int, exclude_booking_id: int = 0,
) -> int:
    """Считает количество завершённых визитов клиента."""
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
    """Возвращает процент скидки за лояльность."""
    discount = 0
    for min_visits, pct in Config.LOYALTY_DISCOUNT_TIERS:
        if completed_visits >= min_visits:
            discount = pct
    return discount


def calculate_booking_price(booking: Booking) -> int:
    """Сумма услуг записи: основная + дополнительные."""
    from sqlalchemy import inspect as sa_inspect
    total = 0
    if "service" not in sa_inspect(booking).unloaded and booking.service:
        total += booking.service.price or 0
    for item in _parse_extra_services(booking):
        total += int(item.get("price") or 0)
    return total


def _parse_extra_services(booking: Booking) -> list:
    """Парсит JSON дополнительных услуг."""
    import json
    if not booking.extra_services_json:
        return []
    try:
        return json.loads(booking.extra_services_json)
    except (json.JSONDecodeError, TypeError):
        return []
