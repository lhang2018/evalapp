import json, urllib.request
def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8300"+path, timeout=10) as r:
        return json.loads(r.read().decode())
data = get("/tasks")
print("keys", list(data)[:8] if isinstance(data, dict) else type(data))
tasks = data.get("tasks") if isinstance(data, dict) else data
print("count", len(tasks))
for t in tasks:
    goal = t.get("user_goal") or ""
    if "calculate" in goal or "5+6" in goal or "1+1" in goal:
        print(json.dumps({k: t.get(k) for k in ("task_id","user_goal","task_status","intent","created_at","failure_reason")}, ensure_ascii=False))
print("latest5", [(t.get("task_id"), t.get("task_status"), (t.get("user_goal") or "")[:24]) for t in tasks[:5]])
