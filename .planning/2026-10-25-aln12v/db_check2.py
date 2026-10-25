import sqlite3
conn = sqlite3.connect(r"E:\evalapp\.evoagent\evoagent.db")
rows = conn.execute("SELECT task_id, status, user_goal FROM tasks WHERE user_goal LIKE '%calculate%' ORDER BY created_at DESC LIMIT 6").fetchall()
print("recent calculate tasks:", rows)
