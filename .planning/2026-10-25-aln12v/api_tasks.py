import json, urllib.request
def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8300"+path, timeout=10) as r:
        return json.loads(r.read().decode())
try:
    data = get("/tasks")
    tasks = data.get("tasks") if isinstance(data, dict) else data
    print("count", len(tasks), "latest", [(t.get("task_id"), t.get("task_status")) for t in tasks[:3]])
except Exception as exc:
    print("list error", exc)
