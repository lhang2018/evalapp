import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8300/tasks?limit=200", timeout=10) as r:
    data = json.loads(r.read().decode())
tasks = data.get("tasks") or []
print("backend tasks:", len(tasks))
print("calculate tasks:", [(t["task_id"], t["task_status"], (t.get("user_goal") or "")[:16]) for t in tasks if "calculate" in (t.get("user_goal") or "")])
