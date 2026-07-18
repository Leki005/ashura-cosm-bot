import re, os

files = []
for root, dirs, fnames in os.walk('handlers'):
    for f in fnames:
        if f.endswith('.py'):
            files.append(os.path.join(root, f))
files.append('keyboards.py')
files.append('bot.py')

patterns = set()
for fpath in files:
    with open(fpath, encoding='utf-8') as f:
        content = f.read()
    for m in re.finditer(r'callback_data\s*=\s*f?"([^"]*)"', content):
        val = m.group(1)
        if '{' not in val:
            patterns.add(val)
    for m in re.finditer(r"F\.data\s*==\s*\"([^\"]+)\"", content):
        patterns.add(m.group(1))
    for m in re.finditer(r"F\.data\.startswith\(\"([^\"]+)\"\)", content):
        patterns.add(m.group(1) + '*')
    for m in re.finditer(r"F\.data\.in_\(\{([^}]+)\}\)", content):
        for item in re.findall(r'"([^"]+)"', m.group(1)):
            patterns.add(item)

for p in sorted(patterns):
    clean = p.rstrip('*')
    bytelen = len(clean.encode('utf-8'))
    flag = ' !!!OVER 64!!!' if bytelen > 64 else ''
    print(f'{bytelen:3d} bytes  {p}{flag}')
