import json, os, glob, datetime
BASE = r'chat/原稿归档/2026-10-25/source1/chatgpt.com/backend-api/conversations/6ad73862-bbcc-83ee-9c54-27de05b3cf6c'
seen = {}
for p in glob.glob(os.path.join(BASE, 'messages*')):
    try: d = json.load(open(p, encoding='utf-8'))
    except Exception: continue
    for m in d.get('messages', []) or []:
        mid = m.get('id')
        if not mid or mid in seen: continue
        seen[mid] = m
OUT = '.planning/2026-10-25-product-baseline/big'
os.makedirs(OUT, exist_ok=True)
rows = []
for mid, m in sorted(seen.items(), key=lambda kv: kv[1].get('create_time') or 0):
    c = m.get('content') or {}
    t = c.get('text')
    if not isinstance(t, str) or len(t) < 2500: continue
    ts = m.get('create_time') or 0
    when = datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d_%H%M') if ts else 'unknown'
    fn = f'{when}_{mid[:8]}.txt'
    open(os.path.join(OUT, fn), 'w', encoding='utf-8').write(t)
    head = ' '.join(t[:300].split())
    rows.append((when, len(t), fn, head))
print('extracted', len(rows))
for r in rows:
    print('%-17s %7d  %-28s %s' % r)
