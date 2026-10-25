import hashlib, json, sqlite3
pre = json.load(open(r"E:\evalapp\.planning\2026-10-25-aln12v\prereboot-state.json", encoding="utf-8"))
conn = sqlite3.connect(r"E:\evalapp\.planning\2026-10-25-aln12v\RKStorage-postboot.db")
raw = conn.execute("SELECT value FROM catalystLocalStorage WHERE key='offline_state_v1'").fetchone()[0]
raw = raw.decode("utf-8") if isinstance(raw, bytes) else raw
state = json.loads(raw)
pre_statuses = {t["local_task_id"]: t["status"] for t in pre["tasks"]}
post_statuses = {t.get("local_task_id"): t.get("status") for t in (state.get("tasks") or [])}
post = {
    "state_digest": hashlib.sha256(raw.encode()).hexdigest(),
    "state_bytes": len(raw),
    "task_count": len(post_statuses),
    "statuses": post_statuses,
    "queue": state.get("queue") or [],
    "dead_letters": len(state.get("dead_letters") or []),
}
comparison = {
    "pre_digest": pre["state_digest"], "post_digest": post["state_digest"],
    "task_ids_preserved": set(pre_statuses) == set(post_statuses),
    "pre_task_count": len(pre_statuses), "post_task_count": len(post_statuses),
    "status_changes": {k: [pre_statuses.get(k), v] for k, v in post_statuses.items()
                        if pre_statuses.get(k) != v},
    "queue_preserved": pre["queue"] == post["queue"],
}
payload = {"pre": {"digest": pre["state_digest"], "tasks": pre["tasks"],
                   "queue": pre["queue"], "dead_letters": pre["dead_letters"]},
           "post": post, "comparison": comparison}
open(r"E:\evalapp\.planning\2026-10-25-aln12v\reboot-compare.json", "w", encoding="utf-8").write(
    json.dumps(payload, ensure_ascii=False, indent=1))
print("digest_equal:", comparison["pre_digest"] == comparison["post_digest"])
print("task_ids_preserved:", comparison["task_ids_preserved"],
      "counts", comparison["pre_task_count"], "->", comparison["post_task_count"])
print("status_changes:", comparison["status_changes"])
print("queue_preserved:", comparison["queue_preserved"], "dead_letters:", post["dead_letters"])
