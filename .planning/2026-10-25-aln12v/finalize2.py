import hashlib, json, os
BASE = r"E:\evalapp\.planning\2026-10-25-aln12v"
path = os.path.join(BASE, "device-matrix-evidence.json")
data = json.load(open(path, encoding="utf-8"))
artifacts = {}
for name in sorted(os.listdir(BASE)):
    full = os.path.join(BASE, name)
    if not os.path.isfile(full) or name == "device-matrix-evidence.json":
        continue
    if name.endswith((".png", ".json", ".db", ".xml")) or name in {"uiop.py", "ui_nodes.py", "submit_task.py"}:
        artifacts[name] = {"sha256": hashlib.sha256(open(full, "rb").read()).hexdigest(),
                           "bytes": os.path.getsize(full)}
data["artifacts"] = artifacts
data["artifact_count"] = len(artifacts)
data["deleted_artifacts_note"] = ("index.bundle.js(6558625B, sha256 a0eb06f5...)、"
                                  "online2.png 等大体积中间产物在证据归档前删除，哈希见本文件 app.js_bundle_sha256")
json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("artifacts", len(artifacts))
print(json.dumps({k: data[k] for k in ("notification_probe", "reboot_recovery")}, ensure_ascii=False)[:900])
