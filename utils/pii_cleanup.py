import logging
from datetime import datetime, timedelta
from sqlalchemy import select, update
from database import async_session, Booking, Review, BonusTransaction
from utils.helpers import now_salon

logger = logging.getLogger(__name__)

# PII retention periods
BOOKING_ANONYMIZE_DAYS = 365  # Anonymize completed/cancelled bookings after 1 year
REVIEW_ANONYMIZE_DAYS = 730   # Anonymize reviews after 2 years

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
    async def run_all() -> dict:
        """Run all PII cleanup tasks."""
        try:
            return {
                'bookings_anonymized': await PIICleanup.anonymize_old_bookings(),
            }
        except Exception:
            logger.exception("PIICleanup.run_all: непредвиденная ошибка")
            return {'bookings_anonymized': 0, 'error': True}
