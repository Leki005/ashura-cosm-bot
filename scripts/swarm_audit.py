"""
SWARM AUDIT — Kimi K3 + Grok параллельный аудит бота.
Запуск: python scripts/swarm_audit.py
Отдельно от бота. Только для аудита.
"""

import asyncio
import sys
import os
import json
from pathlib import Path

# Путь к боту
BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))
os.chdir(str(BOT_DIR))

from utils.swarm import ask_swarm_parallel


def read_critical_code() -> str:
    """Читает критические участки кода для аудита."""
    sections = []

    files_to_check = [
        ("handlers/client.py", [
            ("_finalize_booking", 640, 780),
            ("merge_separate", 1305, 1350),
            ("anamnesis_answer", 298, 355),
        ]),
        ("handlers/admin.py", [
            ("_send_broadcast", 1210, 1260),
            ("AdminOnlyMiddleware", 87, 115),
        ]),
        ("handlers/privacy.py", [
            ("privacy_revoke", 265, 380),
        ]),
        ("handlers/ai_consultant.py", [
            ("ai_consultant_message", 178, 225),
        ]),
        ("handlers/skin_anamnesis.py", [
            ("_show_final", 898, 930),
        ]),
        ("bot.py", [
            ("ThrottlingMiddleware", 195, 220),
            ("on_error", 678, 700),
        ]),
        ("utils/grok.py", [
            ("_check_user_limit", 70, 85),
            ("_sanitize_user_message", 86, 103),
        ]),
        ("utils/privacy.py", [
            ("has_pd_consent", 47, 55),
        ]),
    ]

    for filepath, functions in files_to_check:
        full_path = BOT_DIR / filepath
        if not full_path.exists():
            continue
        lines = full_path.read_text(encoding="utf-8").splitlines()
        for func_name, start, end in functions:
            chunk = lines[start-1:end]
            sections.append(f"=== {filepath}:{start}-{end} ({func_name}) ===")
            sections.extend(chunk)
            sections.append("")

    return "\n".join(sections)


def read_test_results() -> str:
    """Запускает тесты и возвращает результат."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line"],
        capture_output=True, text=True, cwd=str(BOT_DIR)
    )
    return result.stdout + result.stderr


AUDIT_PROMPT = """Ты — Senior QA Engineer уровня Staff+ с 12-летним опытом.
Ты проверяешь Telegram-бота AshuraCosm (косметологический салон).

ПРОВЕРЬ эти 10 пунктов в коде ниже. Для каждого — PASS или FAIL + номер строки + объяснение:

1. ThrottlingMiddleware — правильный извлечение user_id из Update?
2. merge_separate — создаёт ли Booking? (не должен)
3. skip_active_booking_check — есть True где-то? (не должно быть)
4. notify_ok flag — используется в _finalize_booking?
5. session.commit() ДО success сообщения?
6. _finalizing_users guard — есть try/finally?
7. anamnesis_token — во всех местах где нужен?
8. html_esc — во всех местах с user input в HTML?
9. PD revoke — name/phone сохранены ДО анонимизации?
10. AI limits — per-user + semaphore?

Отвечай строго в формате:
[1] PASS/FAIL — file:line — объяснение
[2] PASS/FAIL — file:line — объчинение
...

НИЧЕГО не выдумывай. Только факты из кода."""


async def run_swarm_audit():
    """Запускает параллельный аудит через Grok + Kimi K3."""
    print("=" * 60)
    print("  SWARM AUDIT — Grok + Kimi K3 параллельный аудит")
    print("=" * 60)
    print()

    # Читаем код
    code = read_critical_code()
    print(f"[INFO] Код загружен: {len(code)} символов")

    # Запускаем тесты
    print("[INFO] Запускаю тесты...")
    test_output = read_test_results()
    print(test_output)
    print()

    # Запускаем swarm аудит
    print("[INFO] Отправляю в Grok + Kimi K3 (swarm mode)...")
    print()

    history = [{
        "role": "user",
        "content": f"{AUDIT_PROMPT}\n\n\nКОД БОТА:\n{code}"
    }]

    result = await ask_swarm_parallel(
        history=history,
        system_prompt="Ты — Staff+ QA Engineer. Проверяй код внимательно. Отвечай на русском. Ничего не выдумывай."
    )

    print("─" * 60)
    print("  GROK (xAI) — РЕЗУЛЬТАТ АУДИТА")
    print("─" * 60)
    grok_result = result.get("grok", "NO RESPONSE")
    if grok_result and not grok_result.startswith("ERROR"):
        print(grok_result)
    else:
        print(f"[ОШИБКА] {grok_result}")

    print()
    print("─" * 60)
    print("  KIMI K3 (Moonshot) — РЕЗУЛЬТАТ АУДИТА")
    print("─" * 60)
    kimi_result = result.get("kimi", "NO RESPONSE")
    if kimi_result and not kimi_result.startswith("ERROR"):
        print(kimi_result)
    else:
        print(f"[ОШИБКА] {kimi_result}")

    print()
    print("=" * 60)
    print("  АУДИТ ЗАВЕРШЁН")
    print("=" * 60)

    return result


if __name__ == "__main__":
    asyncio.run(run_swarm_audit())
