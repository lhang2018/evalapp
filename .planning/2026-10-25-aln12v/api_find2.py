import json, urllib.request
def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8300"+path, timeout=10) as r:
        return json.loads(r.read().decode())
for limit in (50, 200):
    data = get(f"/tasks?limit={limit}")
    tasks = data.get("tasks") if isinstance(data, dict) else data
    hits = [t for t in tasks if "calculate" in (t.get("user_goal") or "")]
    print("limit", limit, "count", len(tasks), "calculate-hits", [(t.get("task_id"), t.get("task_status")) for t in hits])
    if limit == 200:
        print("newest", [(t.get("task_id"), t.get("task_status"), (t.get("user_goal") or "")[:20]) for t in tasks[:4]])
