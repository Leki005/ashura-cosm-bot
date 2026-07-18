"""
Очистка мусорных записей в bot.db.
Запустить: python clean_bad_bookings.py
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "bot.db"

if not DB.exists():
    print("bot.db не найден")
    exit(1)

conn = sqlite3.connect(str(DB))
cur = conn.cursor()

# Показываем проблемные записи
print("=== Записи с подозрительными датами ===")
cur.execute("""
    SELECT b.id, b.preferred_date, b.preferred_time, b.status, u.name
    FROM bookings b LEFT JOIN users u ON b.user_id = u.id
    WHERE b.preferred_date NOT LIKE '__.__.____'
       OR b.preferred_date LIKE '%😀%'
       OR b.preferred_date LIKE '%🤣%'
       OR b.preferred_date LIKE '%😂%'
""")
bad = cur.fetchall()
if not bad:
    print("Проблемных записей нет!")
else:
    for row in bad:
        print(f"  #{row[0]} | дата='{row[1]}' | время='{row[2]}' | статус={row[3]} | клиент={row[4]}")

    confirm = input("\nОтменить эти записи? (да/нет): ").strip().lower()
    if confirm in ("да", "yes", "y", "д"):
        for row in bad:
            cur.execute(
                "UPDATE bookings SET status='cancelled' WHERE id=?",
                (row[0],)
            )
            print(f"  #{row[0]} → cancelled")
        conn.commit()
        print("Готово!")
    else:
        print("Отменено, ничего не меняли.")

conn.close()
