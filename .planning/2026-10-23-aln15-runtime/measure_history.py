"""Local history-size diagnostic against a fixed archived source, not a load SLO."""
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[2]
commit = subprocess.check_output(["git", "rev-parse", sys.argv[1]], cwd=root, text=True).strip()
snapshot = root / "outputs" / f"aln15r-{commit[:7]}"
sys.path.insert(0, str(snapshot))
from evoagent.core.persistence import Database
from evoagent.core.budget import BudgetLedger

samples = []
with tempfile.TemporaryDirectory(prefix="history-", dir=root / "outputs") as folder:
    db = Database(str(Path(folder) / "measure.db"))
    service = BudgetLedger(db).reservation_store()
    audit = dict(actor="local-diagnostic", reason="history scan measurement")
    service.create_account("pool", {"cost_micros": 100000, "model_calls": 10000},
                           kind="pool", reference="diagnostic", currency="USD", **audit)
    start = time.perf_counter()
    for i in range(1, 1001):
        reservation = service.reserve("pool", str(i), {"cost_micros": 1, "model_calls": 1},
                                      ttl_seconds=60, **audit)
        service.mark_dispatched(reservation["reservation_id"], **audit)
        service.settle(reservation["reservation_id"], reservation["amounts"], evidence_ref=str(i), **audit)
        if i in {10, 100, 1000}:
            timing = []
            for _ in range(50):
                tick = time.perf_counter()
                balance = service.balance("pool")
                timing.append((time.perf_counter() - tick) * 1000)
            assert balance["spent"]["model_calls"] == i
            timing.sort()
            samples.append(dict(history=i, reads=50, median_ms=statistics.median(timing),
                                p95_ms=timing[47]))
    elapsed = time.perf_counter() - start
    db.close()
record = dict(source_commit=commit, python=platform.python_version(),
    scope="local single-process one account, warm cache, sequential settled history; no production concurrency/SLO claim",
    samples=samples, elapsed_seconds=elapsed)
(Path(__file__).parent / "history-measurement.json").write_text(
    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
print(json.dumps(record, ensure_ascii=False, indent=2))
