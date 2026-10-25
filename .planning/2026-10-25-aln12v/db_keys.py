import sqlite3
conn = sqlite3.connect(r"E:\evalapp\.planning\2026-10-25-aln12v\RKStorage-prereboot.db")
rows = conn.execute("SELECT key, value FROM catalystLocalStorage ORDER BY key").fetchall()
for key, value in rows:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    print(key, "|len", len(text), "|", text[:100].replace("\n", " "))
