"""
SWARM AUDIT — Grok + Kimi K3 параллельный аудит бота.
Запуск: python scripts/swarm_audit.py
Отдельно от бота. Только для аудита.
"""

import asyncio
import sys
import os
import subprocess
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))
os.chdir(str(BOT_DIR))


def read_critical_code() -> str:
    """Читает ключевые файлы бота для аудита."""
    sections = []
    # grok.py исключён — содержит injection patterns, блокируется _sanitize_user_message
    full_files = [
        "handlers/client.py",
        "handlers/admin.py",
        "handlers/privacy.py",
        "handlers/ai_consultant.py",
        "handlers/skin_anamnesis.py",
        "bot.py",
        "utils/helpers.py",
        "utils/privacy.py",
        "config.py",
        "database.py",
        "keyboards.py",
    ]
    for filepath in full_files:
        full_path = BOT_DIR / filepath
        if not full_path.exists():
            continue
        content = full_path.read_text(encoding="utf-8")
        sections.append(f"=== {filepath} ({len(content)} chars) ===")
        sections.append(content)
        sections.append("")
    return "\n".join(sections)


def read_test_results() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line"],
        capture_output=True, text=True, cwd=str(BOT_DIR)
    )
    return result.stdout + result.stderr


AUDIT_PROMPT = """Проверь 7 пунктов в коде Telegram-бота. Для каждого — PASS или FAIL:

1. ThrottlingMiddleware — user_id из event.event? (Update-level middleware)
2. merge_separate — создаёт Booking?
3. skip_active_booking_check=True — есть?
4. notify_ok — используется в _finalize_booking?
5. session.commit() ДО success сообщения?
6. PD revoke — name/phone ДО anonymize?
7. AI per-user limit + semaphore (_ai_sem)?

Формат: [1] PASS/FAIL — объяснение
Только факты из кода."""


async def run_swarm_audit():
    print("=" * 60)
    print("  SWARM AUDIT — Grok + Kimi K3 параллельный аудит")
    print("=" * 60)
    print()

    code = read_critical_code()
    print(f"[INFO] Код загружен: {len(code)} символов")

    print("[INFO] Запускаю тесты...")
    test_output = read_test_results()
    print(test_output)
    print()

    history = [{"role": "user", "content": f"{AUDIT_PROMPT}\n\n\nКОД БОТА:\n{code}"}]

    # --- Grok (xAI) ---
    print("[INFO] Запускаю Grok (xAI)...")
    try:
        from utils.grok import ask_grok
        grok_result = await ask_grok(
            history=history,
            system_prompt="Ты — Staff+ QA Engineer. Проверяй ВЕСЬ код. Для каждого пункта: PASS или FAIL + file:line. Только факты. На русском.",
        )
    except Exception as e:
        grok_result = f"ERROR: {e}"

    # --- Kimi K3 (kimi-k2.7-code-highspeed — без thinking mode) ---
    print("[INFO] Запускаю Kimi K3...")
    try:
        from utils.kimi import ask_kimi_fast
        kimi_result = await ask_kimi_fast(
            history=history,
            system_prompt="Ты — Staff+ QA Engineer. Проверяй ВЕСЬ код. Для каждого пункта: PASS или FAIL + file:line. Только факты. На русском.",
            max_tokens=4096,
        )
    except Exception as e:
        kimi_result = f"ERROR: {e}"

    # --- Вывод ---
    print("─" * 60)
    print("  GROK (xAI) — РЕЗУЛЬТАТ АУДИТА")
    print("─" * 60)
    if grok_result and not grok_result.startswith("ERROR"):
        print(grok_result[:4000])
    else:
        print(f"[ОШИБКА] {grok_result}")

    print()
    print("─" * 60)
    print("  KIMI K3 (Moonshot) — РЕЗУЛЬТАТ АУДИТА")
    print("─" * 60)
    if kimi_result and not kimi_result.startswith("ERROR"):
        print(kimi_result[:4000])
    else:
        print(f"[ОШИБКА] {kimi_result}")

    print()
    print("=" * 60)
    print("  АУДИТ ЗАВЕРШЁН")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_swarm_audit())
