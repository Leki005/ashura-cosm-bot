from sqlalchemy import text
from database import async_session


async def check_db() -> bool:
    try:
        async with async_session() as session:
            await session.execute(text('SELECT 1'))
            return True
    except Exception:
        return False


async def check_bot(bot) -> bool:
    try:
        me = await bot.get_me()
        return me is not None
    except Exception:
        return False
