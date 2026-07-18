"""Kimi code review - file-based approach"""
import json, urllib.request, os

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
FILES = ['bot.py', 'database.py', 'config.py', 'handlers/client.py', 'handlers/admin.py', 'utils/grok.py', 'utils/helpers.py']

parts = []
for f in FILES:
    path = os.path.join(BOT_DIR, f)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as fh:
            content = fh.read()[:40000]
            parts.append(f'\n=== {f} ===\n{content}')

code = '\n'.join(parts)
print(f'Code loaded: {len(code)} chars')

system_msg = 'Ты — Kimi, ИИ-ассистент для код-ревью. Отвечай на русском.'
user_msg = 'Проведи ревью этого Telegram-бота на aiogram 3. Найди ТОП-10 критичных проблем (race conditions, безопасность, утечки памяти). Для каждой: файл, строка, что не так, как исправить.\n\nКОД БОТА:\n' + code

payload = json.dumps({
    'model': 'kimi-for-coding',
    'messages': [
        {'role': 'system', 'content': system_msg},
        {'role': 'user', 'content': user_msg}
    ],
    'max_tokens': 4000,
    'thinking': {'type': 'disabled'},
}, ensure_ascii=False).encode('utf-8')

print(f'Payload: {len(payload)} bytes')

req = urllib.request.Request('https://api.kimi.com/coding/v1/chat/completions', data=payload, headers={
    'Authorization': 'Bearer sk-kimi-NEpA41cB8vg4GacTCdIeHxQnndxuJ1PJ80Fva0b826cV0kc1i5DzMLrBvkEEw7tM',
    'Content-Type': 'application/json',
})
with urllib.request.urlopen(req, timeout=180) as resp:
    data = json.loads(resp.read().decode('utf-8'))
    result = data['choices'][0]['message']['content']
    print(result)

    out_path = os.path.join(BOT_DIR, 'REVIEW_KIMI.txt')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(result)
    print(f'\n[Saved to {out_path}]')
