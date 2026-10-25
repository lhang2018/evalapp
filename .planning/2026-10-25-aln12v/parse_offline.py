import hashlib, json, sqlite3
conn = sqlite3.connect(r"E:\evalapp\.planning\2026-10-25-aln12v\RKStorage-prereboot.db")
row = conn.execute("SELECT value FROM catalystLocalStorage WHERE key='offline_state_v1'").fetchone()
raw = row[0].decode("utf-8") if isinstance(row[0], bytes) else row[0]
state = json.loads(raw)
tasks = state.get("tasks") or []
queue = state.get("queue") or []
summary = {
    "state_digest": hashlib.sha256(raw.encode()).hexdigest(),
    "state_bytes": len(raw),
    "tasks": [{"local_task_id": t.get("local_task_id"), "status": t.get("status"),
               "goal": t.get("goal") or t.get("user_goal"),
               "server_task_id": t.get("server_task_id"),
               "current_run_id": t.get("current_run_id")} for t in tasks],
    "queue": [{"event_id": q.get("event_id") or q.get("id"),
               "kind": q.get("kind") or q.get("event_type"),
               "state": q.get("state") or q.get("status")} for q in queue],
    "checkpoint": state.get("checkpoint"),
    "dead_letters": len(state.get("dead_letters") or []),
}
path = r"E:\evalapp\.planning\2026-10-25-aln12v\prereboot-state.json"
open(path, "w", encoding="utf-8").write(json.dumps(summary, ensure_ascii=False, indent=1))
print("digest", summary["state_digest"][:16], "tasks", len(tasks), "queue", len(queue), "checkpoint", summary["checkpoint"])
print("task ids:", [(t["local_task_id"], t["status"], (t["goal"] or "")[:16]) for t in summary["tasks"][:8]])
print("queue:", summary["queue"][:5])
