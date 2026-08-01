import logging
from datetime import datetime, timedelta
from sqlalchemy import select, update
from database import async_session, Booking, Review, BonusTransaction, User
from utils.helpers import now_salon

logger = logging.getLogger(__name__)

# PII retention periods
BOOKING_ANONYMIZE_DAYS = 365  # Anonymize completed/cancelled bookings after 1 year
REVIEW_ANONYMIZE_DAYS = 730   # Anonymize reviews after 2 years
USER_ANONYMIZE_DAYS = 365 * 2  # Anonymize inactive users after 2 years

class PIICleanup:
    """Periodic PII cleanup for 152-ФЗ compliance."""

    @staticmethod
    async def anonymize_old_bookings() -> int:
        """Anonymize PII in old completed/cancelled bookings."""
        cutoff = now_salon() - timedelta(days=BOOKING_ANONYMIZE_DAYS)
        async with async_session() as session:
            result = await session.execute(
                update(Booking)
                .where(
                    Booking.status.in_(('completed', 'cancelled')),
                    Booking.created_at < cutoff,
                    Booking.anamnesis_json.isnot(None),
                )
                .values(anamnesis_json=None, notes=None)
            )
            await session.commit()
            count = result.rowcount
            if count:
                logger.info('PII cleanup: anonymized %d old bookings', count)
            return count

    @staticmethod
    async def anonymize_old_reviews() -> int:
        """Anonymize PII in old reviews by clearing text."""
        cutoff = now_salon() - timedelta(days=REVIEW_ANONYMIZE_DAYS)
        async with async_session() as session:
            result = await session.execute(
                update(Review)
                .where(
                    Review.created_at < cutoff,
                    Review.text.isnot(None),
                )
                .values(text=None)
            )
            await session.commit()
            count = result.rowcount
            if count:
                logger.info('PII cleanup: anonymized %d old reviews', count)
            return count

    @staticmethod
    async def anonymize_inactive_users() -> int:
        """Anonymize PII in inactive users (name/phone) after retention period."""
        cutoff = now_salon() - timedelta(days=USER_ANONYMIZE_DAYS)
        async with async_session() as session:
            # Only anonymize users with no upcoming bookings
            result = await session.execute(
                update(User)
                .where(
                    User.created_at < cutoff,
                    User.name.isnot(None),
                    User.name.notlike('Удалён_%'),
                    ~User.bookings.any(Booking.status.in_(('pending', 'confirmed'))),
                )
                .values(
                    name='Удалён_',
                    phone=None,
                    username=None,
                    anamnesis_json=None,
                    skin_anamnesis_json=None,
                )
            )
            await session.commit()
            count = result.rowcount
            if count:
                logger.info('PII cleanup: anonymized %d inactive users (name/phone)', count)
            return count

    @staticmethod
    async def run_all() -> dict:
        """Run all PII cleanup tasks."""
        try:
            return {
                'bookings_anonymized': await PIICleanup.anonymize_old_bookings(),
                'reviews_anonymized': await PIICleanup.anonymize_old_reviews(),
                'users_anonymized': await PIICleanup.anonymize_inactive_users(),
            }
        except Exception:
            logger.exception("PIICleanup.run_all: непредвиденная ошибка")
            return {'bookings_anonymized': 0, 'reviews_anonymized': 0, 'users_anonymized': 0, 'error': True}
