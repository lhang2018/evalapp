import json, os, glob, datetime, sys
BASE = r'chat/原稿归档/2026-10-25/source1/chatgpt.com/backend-api/conversations/6ad73862-bbcc-83ee-9c54-27de05b3cf6c'
files = glob.glob(os.path.join(BASE, 'messages*')) + glob.glob(os.path.join(BASE, 'conversations*'))
msgs = {}
def walk_text(o, acc):
    if isinstance(o, dict):
        if isinstance(o.get('text'), str): acc.append(o['text'])
        for k, v in o.items():
            if k != 'text': walk_text(v, acc)
    elif isinstance(o, list):
        for v in o: walk_text(v, acc)
for p in files:
    try:
        d = json.load(open(p, encoding='utf-8'))
    except Exception as e:
        print('ERR', os.path.basename(p)[:50], e); continue
    for msg in d.get('messages', []) or []:
        mid = msg.get('id')
        if not mid: continue
        content = msg.get('content') or {}
        acc = []
        parts = content.get('parts') or []
        for x in parts:
            if isinstance(x, dict) and isinstance(x.get('text'), str): acc.append(x['text'])
        if not acc:
            raw = content.get('text')
            if isinstance(raw, str):
                if raw.strip().startswith('{') or raw.strip().startswith('['):
                    try: walk_text(json.loads(raw), acc)
                    except Exception: acc.append(raw)
                else:
                    acc.append(raw)
        txt = ''.join(acc)
        if not txt.strip(): continue
        ts = msg.get('create_time') or 0
        if mid not in msgs or len(txt) > len(msgs[mid][1]):
            msgs[mid] = (ts, txt, (msg.get('author') or {}).get('role'))
out = sorted(msgs.items(), key=lambda kv: kv[1][0])
recs = []
for mid, (ts, txt, role) in out:
    t = datetime.datetime.utcfromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '??'
    recs.append((t, role or '?', len(txt), mid, txt[:80].replace('\n', ' ')))
with open('.planning/2026-10-25-product-baseline/index.txt', 'w', encoding='utf-8') as f:
    for r in recs:
        f.write('%s | %-9s | %6d | %s | %s\n' % r)
# full dump
with open('.planning/2026-10-25-product-baseline/full.txt', 'w', encoding='utf-8') as f:
    for mid, (ts, txt, role) in out:
        t = datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else '??'
        f.write('\n\n<<<<<MSG %s | %s | %s | len=%d>>>>>\n%s\n' % (mid, t, role, len(txt), txt))
print('recovered', len(out))
for r in recs[-45:]:
    print('%s | %-9s | %6d | %s | %s' % r)
