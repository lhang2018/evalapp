"""Export only the test namespace from the stopped app's private DB."""
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
name = sys.argv[1]
assert name in {"before-restart", "after-restart", "final"}
private = root / "outputs/baseline-device-f550d90/device-private-backup"
raw = subprocess.check_output(["adb", "-s", "5748d823", "exec-out", "run-as", "com.evoagent", "cat", "databases/RKStorage"])
db = private / (name + ".db")
db.write_bytes(raw)
with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
    rows = dict(connection.execute("SELECT key,value FROM catalystLocalStorage WHERE key LIKE ?",
        ("review_f550d90_20261024_v2/%",)))
prefix = "review_f550d90_20261024_v2/"
report = json.loads(rows[prefix + "report"])
report["capture"] = dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(), device="5748d823 / 24018RPACC / Android16",
    raw_db_sha256=hashlib.sha256(raw).hexdigest(), raw_db_committed=False)
report["namespace_rows"] = len(rows)
report["durable_state"] = {key[len(prefix):]: json.loads(value) for key, value in rows.items()
    if key[len(prefix):] in {"ledger/device_task_ledger_v1", "update/update_state_v1", "update/bundle_runtime_v1"}}
(evidence / ("device-" + name + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
print(json.dumps(dict(phase=report.get("phase"), tests=len(report["tests"]), passed=sum(t["ok"] for t in report["tests"])), indent=2))
