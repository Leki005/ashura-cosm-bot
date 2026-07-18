"""
GRILL-ME Review через Grok API.
Отправляет критические секции кода на повторную проверку.
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from utils.grok import ask_grok

CRITICAL_FILES = [
    "handlers/admin.py",
    "handlers/client.py", 
    "handlers/privacy.py",
    "handlers/common.py",
    "handlers/ai_consultant.py",
    "handlers/skin_anamnesis.py",
    "utils/grok.py",
    "utils/helpers.py",
    "database.py",
    "bot.py",
    "config.py",
    "keyboards.py",
]

GRILL_PROMPT = """Ты — Senior Security Engineer и Python-разработчик с 15-летним стажем. 
Твоя задача — НАЙТИ ВСЁ ЧТО СЛОМАЕТСЯ в продакшене.

Вот уже найденные критические баги от предыдущего аудита. ПРОВЕРЬ КАЖДЫЙ и оцени:

1. CALLBACK DATA COLLISION: admin_accept_quick_ vs admin_accept_ (admin.py)
2. BROADCAST SENDS RAW HTML без валидации (admin.py)  
3. RACE CONDITION на активную запись без DB lock (client.py)
4. MIDDLEWARE BYPASSES privacy check если session=None (privacy.py)
5. AI_HISTORY растёт бесконечно — memory/cost bomb (ai_consultant.py)
6. SQL INJECTION в apply_migrations через f-string (database.py)
7. SSL VERIFICATION отключена по умолчанию (config.py)
8. MemoryStorage теряет FSM state при рестарте (bot.py)
9. TOCTOU race на создании записи (client.py)
10. Даты хранятся как строки String(100) (database.py)
11. Нет log rotation — файл растёт бесконечно (bot.py)
12. catch-all handler может перехватывать чужие FSM состояния (client.py)
13. privacy_revoke не отменяет активные записи (privacy.py)
14. lru_cache на промптах не инвалидируется (grok.py)
15. _daily_requests не персистится при рестарте (grok.py)

Для каждого бага:
- Подтверди или опровергни (с обоснованием)
- Оцени severity: CRITICAL / HIGH / MEDIUM / LOW
- Дай конкретный fix (код, не общие слова)
- Найди ЛЮБЫЕ дополнительные баги которые пропустили

Формат: [GRILL-N] severity | файл:строка | описание | fix
Будь максимально жёстким. Не жалей код."""

async def main():
    print("=" * 60)
    print("[GRILL-ME] Отправляю код на проверку Grok...")
    print("=" * 60)
    
    # Читаем только критические файлы (сокращённо)
    code_parts = []
    for fname in CRITICAL_FILES:
        fpath = os.path.join(os.path.dirname(__file__), fname)
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            # Берём первые 3000 символов каждого файла для экономии токенов
            if len(content) > 3000:
                content = content[:3000] + f"\n... [обрезано, всего {len(content)} символов]"
            code_parts.append(f"\n{'='*40}\nFILE: {fname}\n{'='*40}\n{content}")
    
    full_code = "\n".join(code_parts)
    
    prompt = f"{GRILL_PROMPT}\n\nКОД ПРОЕКТА:\n{full_code}"
    
    try:
        response = await ask_grok(
            history=[{"role": "user", "content": prompt}],
            system_prompt="Ты — агрессивный code reviewer. Находи баги, не жалей разработчика. Отвечай на русском.",
            model="grok-4.3"
        )
        
        print("\n" + "=" * 60)
        print("[GRILL-ME] ОТВЕТ GROK:")
        print("=" * 60)
        print(response)
        
        # Сохраняем в файл
        output_path = os.path.join(os.path.dirname(__file__), "REVIEW_GRILL_GROK.txt")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"GRILL-ME REVIEW — Grok 4.3\n{'='*60}\n\n{response}")
        print(f"\nСохранено в: {output_path}")
        
    except Exception as e:
        print(f"[ОШИБКА] Grok API: {e}")

if __name__ == "__main__":
    asyncio.run(main())
