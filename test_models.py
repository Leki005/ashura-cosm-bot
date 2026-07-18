import asyncio, sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv('.env')
from utils.grok import ask_grok

async def test():
    models = ['grok-4.5', 'grok-4.3', 'grok-4', 'grok-4-latest']
    for m in models:
        try:
            r = await ask_grok(
                history=[{'role': 'user', 'content': 'Say OK'}],
                system_prompt='Reply with one word.',
                model=m
            )
            print(f'{m}: OK -> {r[:30]}')
        except Exception as e:
            err = str(e)[:80]
            print(f'{m}: FAIL -> {err}')

asyncio.run(test())
