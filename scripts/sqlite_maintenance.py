"""
Safe SQLite backup — использует SQLite backup API вместо shutil.copy2.
"""
import sqlite3
import os
import shutil
from pathlib import Path
from datetime import datetime

DB_PATH = Path(os.getenv('DATABASE_PATH', str(Path(__file__).resolve().parent.parent / 'bot.db')))
if not DB_PATH.exists():
    alt = Path('/app/data/bot.db')
    if alt.exists():
        DB_PATH = alt
BACKUP_DIR = DB_PATH.parent / "backups"
MAX_BACKUPS = 10  # Хранить последние 10 бэкапов


def safe_backup():
    """Безопасный бэкап через SQLite backup API."""
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return False

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"bot_{ts}.db"

    try:
        src_conn = sqlite3.connect(str(DB_PATH))
        dst_conn = sqlite3.connect(str(dst))
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
        print(f"Backup OK: {dst} ({dst.stat().st_size} bytes)")
    except Exception as e:
        print(f"Backup FAILED: {e}")
        return False

    # Удаляем старые бэкапы
    backups = sorted(BACKUP_DIR.glob("bot_*.db"), key=lambda p: p.stat().st_mtime)
    while len(backups) > MAX_BACKUPS:
        old = backups.pop(0)
        old.unlink()
        print(f"Removed old backup: {old.name}")

    return True


def vacuum_db():
    """VACUUM — дефрагментация и уменьшение размера БД."""
    if not DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("VACUUM")
        conn.close()
        size = DB_PATH.stat().st_size
        print(f"VACUUM OK: {size} bytes")
        return True
    except Exception as e:
        print(f"VACUUM FAILED: {e}")
        return False


def check_integrity():
    """Проверка целостности БД."""
    if not DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(str(DB_PATH))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        ok = result[0] == "ok"
        print(f"Integrity: {result[0]}")
        return ok
    except Exception as e:
        print(f"Integrity check FAILED: {e}")
        return False


if __name__ == "__main__":
    print("=== SQLite Maintenance ===")
    print("\n1. Integrity check:")
    check_integrity()

    print("\n2. Backup:")
    safe_backup()

    print("\n3. VACUUM:")
    vacuum_db()

    print("\n4. DB size:")
    if DB_PATH.exists():
        size_mb = DB_PATH.stat().st_size / (1024 * 1024)
        print(f"   {size_mb:.2f} MB")
    print("\nDone!")
