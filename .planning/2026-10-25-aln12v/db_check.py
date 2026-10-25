import sqlite3, json
conn = sqlite3.connect(r"E:\evalapp\.evoagent\evoagent.db")
rows = conn.execute("SELECT task_id, status, user_goal, data FROM tasks WHERE user_goal LIKE '%calculate%' ORDER BY created_at DESC LIMIT 10").fetchall()
print(len(rows))
for r in rows:
    print(r[0], r[1], r[2])
rows2 = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
print("total tasks in .evoagent/evoagent.db:", rows2[0])
