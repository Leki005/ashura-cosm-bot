#!/usr/bin/env python3
"""
Автоматический бэкап БД для cron.
Запуск: python scripts/auto_backup.py
В crontab: 0 3 * * * cd /path/to/cosmetology_bot && python scripts/auto_backup.py >> /var/log/ashura_backup.log 2>&1
"""

import os
import sys
import sqlite3
import shutil
from pathlib import Path
from datetime import datetime

# Определяем путь к БД
DB_PATH = Path(os.getenv('DATABASE_PATH', ''))
if not DB_PATH or not DB_PATH.exists():
    DB_PATH = Path('bot.db')
if not DB_PATH.exists():
    alt = Path('/app/data/bot.db')
    if alt.exists():
        DB_PATH = alt

BACKUP_DIR = DB_PATH.parent / 'backups'
MAX_BACKUPS = 14  # Хранить 2 недели ежедневных бэкапов


def backup():
    if not DB_PATH.exists():
        print(f'[{datetime.now()}] БД не найдена: {DB_PATH}')
        return False

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = BACKUP_DIR / f'bot_{ts}.db'

    try:
        # SQLite backup API — безопасно при активных записях
        src_conn = sqlite3.connect(str(DB_PATH))
        dst_conn = sqlite3.connect(str(dst))
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()

        size_mb = dst.stat().st_size / (1024 * 1024)
        print(f'[{datetime.now()}] Бэкап OK: {dst} ({size_mb:.2f} MB)')
    except Exception as e:
        print(f'[{datetime.now()}] Бэкап FAIL: {e}')
        return False

    # Удаляем старые бэкапы
    backups = sorted(BACKUP_DIR.glob('bot_*.db'), key=lambda p: p.stat().st_mtime)
    removed = 0
    while len(backups) > MAX_BACKUPS:
        old = backups.pop(0)
        old.unlink()
        removed += 1
    if removed:
        print(f'[{datetime.now()}] Удалено старых бэкапов: {removed}')

    # Проверка целостности бэкапа
    try:
        conn = sqlite3.connect(str(dst))
        result = conn.execute('PRAGMA integrity_check').fetchone()
        conn.close()
        if result[0] != 'ok':
            print(f'[{datetime.now()}] ВНИМАНИЕ: бэкап повреждён! integrity_check={result[0]}')
            return False
    except Exception as e:
        print(f'[{datetime.now()}] Проверка целостности FAIL: {e}')
        return False

    return True


if __name__ == '__main__':
    success = backup()
    sys.exit(0 if success else 1)
