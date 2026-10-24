"""Cross-check collected device evidence; do not claim a new device run."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
inputs = []


def attested(relative, ref=None):
    path = root / relative
    blob = subprocess.check_output(["git", "show", ref + ":" + relative], cwd=root) if ref else path.read_bytes()
    inputs.append(dict(path=relative, git_ref=ref, sha256=hashlib.sha256(blob).hexdigest()))
    return blob


approval_nodes = {}
for relative, ref, label in [
    (".planning/2026-10-24-aln04ui-device/a7.xml", "07f11e5", "expired"),
    (".planning/2026-10-24-aln11w-device/f4.xml", None, "offline"),
]:
    nodes = ET.fromstring(attested(relative, ref)).iter("node")
    selected = []
    for node in nodes:
        text = node.get("text") or node.get("content-desc", "")
        if (text.startswith("审批 apr_") or text.startswith("详情待本次") or text.startswith("离线：")
                or text in ("批准此动作", "拒绝并取消任务")):
            selected.append(dict(text=text, enabled=node.get("enabled"), widget=node.get("class")))
    approval_nodes[label] = selected
assert any("已过期" in n["text"] for n in approval_nodes["expired"])
assert any(n["text"].startswith("离线：") for n in approval_nodes["offline"])
assert all(n["enabled"] == "false" for n in approval_nodes["offline"] if n["text"] in ("批准此动作", "拒绝并取消任务"))

for relative, ref in [
    (".planning/2026-10-24-aln04ui-device/final-approve.png", "07f11e5"),
    (".planning/2026-10-24-aln11w-device/ui15.png", "657ed1c"),
    (".planning/2026-10-24-aln11w-device/ui18-restart.png", "657ed1c"),
]:
    attested(relative, ref)

db_path = root / ".evoagent-update/evoagent.db"
conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
conn.execute("BEGIN")
expected = {"a15c5d4718e3": "SUCCEEDED", "65ed5ad7b9f6": "CANCELLED",
            "9f9ce3874a88": "SUCCEEDED", "b0f2e3173c65": "SUSPENDED"}
task_rows = {row[0]: row[1] for row in conn.execute("SELECT task_id,status FROM tasks WHERE task_id IN (?,?,?,?)", tuple(expected))}
assert task_rows == expected
approval_rows = [dict(task_id=t, approval_id=a, raw_status=s, effective_status="expired" if s in ("pending", "approved") and e and e <= time.time() else s)
    for t, a, s, e in conn.execute("SELECT task_id,approval_id,status,expires_at FROM approvals WHERE task_id IN (?,?,?,?)", tuple(expected))]
assert any(r["task_id"] == "65ed5ad7b9f6" and r["raw_status"] == "denied" for r in approval_rows)
assert any(r["task_id"] == "9f9ce3874a88" and r["raw_status"] == "used" for r in approval_rows)
assert any(r["task_id"] == "b0f2e3173c65" and r["effective_status"] == "expired" for r in approval_rows)
conn.close()

snapshot = root / ".planning/2026-10-24-aln11w-device/RKStorage-device.db"
attested(snapshot.relative_to(root).as_posix())
conn = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
kv = {k: json.loads(v) for k, v in conn.execute("SELECT key,value FROM catalystLocalStorage WHERE key IN (?,?,?)",
    ("update_state_v1", "bundle_runtime_v1", "update_bundle_active_v1"))}
conn.close()
update, runtime, active = (kv[k] for k in ("update_state_v1", "bundle_runtime_v1", "update_bundle_active_v1"))
assert update["phase"] == "rolled_back" and update["last_known_good"]["release_id"] == "rel_good_v3"
recovery = runtime["recovery"]
assert recovery["outcome"] == "succeeded" and recovery["execution_id"]
assert recovery["failed"]["release_id"] == "rel_bad_v4" and recovery["recovered"]["release_id"] == "rel_good_v3"
assert active["digest"] == recovery["recovered"]["digest"] == update["last_known_good"]["digest"]
assert any(p["outcome"] == "unhealthy" and p["release_id"] == "rel_bad_v4" for p in runtime["probes"])
assert any(r["execution_id"] == recovery["execution_id"] and r["outcome"] == "succeeded" for r in runtime["records"])

record = dict(scope="Read-only audit of zcode-main's device collection; not a newly executed device scenario",
    device="5748d823 / 24018RPACC / Android16 / debug0.1.0", collector="zcode-main", auditor="codex-runtime",
    provenance_commits=["657ed1c", "07f11e5"], inputs=inputs,
    approval_nodes=approval_nodes, task_rows=task_rows, approval_rows=approval_rows,
    workflow=dict(phase=update["phase"], active=active, recovery=recovery, probes=runtime["probes"],
                  records=[{k:r.get(k) for k in ("execution_id", "task_ref", "release_id", "version", "digest", "workflow_id", "outcome")} for r in runtime["records"]]),
    limits=["Approval UI tasks were mapped with a fixture; auth disabled in this local demo; role/auth boundaries tested separately",
            "Derived expiry is checked without claiming pending SQL rows were transitioned",
            "No new physical reboot, production signing, or natural-language device main-chain claim"])
(folder / "device-evidence-audit.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
print("device evidence: four approval paths and rollback/recovery/restart collection corroborated")
