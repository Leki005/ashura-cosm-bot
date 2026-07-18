"""
Бэкап bot.db — запускать перед обновлениями или по cron.
Создаёт копию bot.db с датой в имени файла.
"""
import os
import shutil
from datetime import datetime
from pathlib import Path

DB_PATH = Path(os.getenv('DATABASE_PATH', str(Path(__file__).parent / 'bot.db')))
if not DB_PATH.exists():
    alt = Path('/app/data/bot.db')
    if alt.exists():
        DB_PATH = alt
BACKUP_DIR = Path(__file__).parent / "backups"


def backup():
    if not DB_PATH.exists():
        print("bot.db не найден")
        return

    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"bot_{ts}.db"
    shutil.copy2(DB_PATH, dst)
    print(f"Бэкап: {dst}")

    # Оставляем последние 10 бэкапов
    backups = sorted(BACKUP_DIR.glob("bot_*.db"), reverse=True)
    for old in backups[10:]:
        old.unlink()
        print(f"Удалён старый: {old.name}")


if __name__ == "__main__":
    backup()
