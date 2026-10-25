import sqlite3, json
conn = sqlite3.connect(r"E:\evalapp\.planning\2026-10-25-aln12v\RKStorage-prereboot.db")
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", tables)
if "catalystLocalStorage" in tables:
    rows = conn.execute("SELECT key, value FROM catalystLocalStorage ORDER BY key").fetchall()
    print("key_count:", len(rows))
    for key, value in rows:
        if any(tag in key for tag in ("task", "queue", "checkpoint", "base_url")):
            text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
            print("KEY", key, "len", len(text))
            if key == "evoagent_base_url":
                print("  ", text)
            if "tasks" in key:
                try:
                    data = json.loads(text)
                    items = data if isinstance(data, list) else data.get("tasks") or data.get("items") or []
                    print("  tasks:", [(t.get("local_task_id"), t.get("status"), (t.get("goal") or t.get("user_goal") or "")[:20]) for t in items][:8])
                except Exception as exc:
                    print("  parse error", exc)
            if "queue" in key:
                try:
                    data = json.loads(text)
                    print("  queue:", str(data)[:300])
                except Exception as exc:
                    print("  parse error", exc)
