"""
Запуск бота (алиас для bot.py).
Используйте: python app.py
Или двойной клик по start.bat
"""

from bot import main
import asyncio
import sys

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
        sys.exit(0)