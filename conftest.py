import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event, text as sql_text

from database import Base


@pytest_asyncio.fixture
async def db_session():
    """Асинхронная сессия с in-memory SQLite для тестов."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Partial unique index (created by apply_migrations in production)
        await conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_booking "
                "ON bookings(user_id) WHERE status IN ('pending', 'confirmed')"
            )
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()
