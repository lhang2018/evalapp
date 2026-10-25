import sqlite3, json
conn = sqlite3.connect(r"E:\evalapp\.planning\2026-10-25-aln12v\RKStorage-prereboot.db")
print("tables:", [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")])
rows = conn.execute("SELECT key, length(value) FROM catalystLocalStorage ORDER BY key").fetchall() if conn.execute("SELECT name FROM sqlite_master WHERE name='catalystLocalStorage'").fetchone() else []
print("keys:", [(k, n) for k, n in rows][:12])
for k, n in rows:
    if "task" in k or "queue" in k or "checkpoint" in k:
        print(k, n)
