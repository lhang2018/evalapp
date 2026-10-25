import json, sys
from lxml import etree
path = sys.argv[1]
tree = etree.parse(path)
rows = []
for node in tree.iter("node"):
    text = (node.get("text") or "").strip()
    desc = (node.get("content-desc") or "").strip()
    cls = node.get("class") or ""
    bounds = node.get("bounds") or ""
    if not (text or desc):
        continue
    rows.append({"class": cls, "text": text, "desc": desc, "bounds": bounds,
                 "clickable": node.get("clickable"), "enabled": node.get("enabled")})
for row in rows[:60]:
    print(json.dumps(row, ensure_ascii=False))
