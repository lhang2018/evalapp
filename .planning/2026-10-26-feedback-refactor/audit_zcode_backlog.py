"""Read-only backlog audit; no product evidence, acceptance, or board mutation."""
from collections import Counter
from datetime import datetime, timezone
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8")


def snapshot(path):
    return git("show", f"{commit}:{path}")


def counts(rows):
    return dict(Counter(row["status"] for row in rows))


commit = git("rev-parse", "HEAD").strip()
board_path = "docs/协作开发/workboard.json"
committed = json.loads(snapshot(board_path))
live_bytes = (ROOT / board_path).read_bytes()
live = json.loads(live_bytes)
tracked_ids = {card["id"] for card in committed["dispatches"]}
owned = [card for card in committed["dispatches"]
         if (card.get("owner") or "").startswith("zcode")]

domains = {name: [] for name in ("deployment", "recovery", "knowledge", "observability")}
entry_paths = ["evoagent/app.py", "evoagent/cli.py"]
entry_paths += git("ls-tree", "-r", "--name-only", commit, "evoagent/api").splitlines()
for path in entry_paths:
    if not path.endswith(".py"):
        continue
    tree = ast.parse(snapshot(path), filename=path)
    for node in ast.walk(tree):
        module = ""
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
        elif isinstance(node, ast.Import):
            module = " ".join(alias.name for alias in node.names)
        for domain in domains:
            if domain in module.split("."):
                domains[domain].append({"path": path, "line": node.lineno,
                                        "module": module})

checks = {}
for name, args in {
    "product_matrix": ["tools/check_product_acceptance.py", "--source-commit", commit],
    "product_required": ["tools/check_product_acceptance.py", "--source-commit", commit, "--require-pass"],
    "release_structure": ["tools/check_release_gates.py", "--json"],
    "release_required": ["tools/check_release_gates.py", "--json", "--require-release"],
}.items():
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", *args], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8")
    log = OUT / f"zcode-{name}.json"
    log.write_text(result.stdout, encoding="utf-8")
    checks[name] = {"arguments": args, "exit_code": result.returncode,
                    "output": log.relative_to(ROOT).as_posix(),
                    "stderr": result.stderr}
    assert result.returncode == {"product_matrix": 0, "product_required": 1,
                                  "release_structure": 0, "release_required": 2}[name]

report = {
    "scope": "documentation inventory and static import audit; not functional acceptance",
    "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    "source_commit": commit,
    "committed_board": {"workpackages": len(committed["workpackages"]),
                        "dispatches": len(committed["dispatches"]),
                        "counts": counts(committed["dispatches"]),
                        "baseline_status": committed["baseline"]["status"],
                        "published_commit": committed["baseline"].get("published_commit")},
    "zcode_owned_committed": {"count": len(owned), "counts": counts(owned),
                               "cards": [{k: card.get(k) for k in
                                          ("id", "title", "owner", "status", "delivery", "next_step")}
                                         for card in owned]},
    "workspace_board": {"sha256": hashlib.sha256(live_bytes).hexdigest(),
                        "workpackages": len(live["workpackages"]),
                        "dispatches": len(live["dispatches"]),
                        "counts": counts(live["dispatches"]),
                        "additional_cards": [{k: card.get(k) for k in ("id", "owner", "status", "card")}
                                             for card in live["dispatches"] if card["id"] not in tracked_ids],
                        "differs_from_committed_board": live != committed,
                        "note": "Workspace and committed board counted separately; this audit does not mutate or commit other people's board changes"},
    "entry_domain_imports": domains,
    "import_limit": "Direct import edges only; neither proof of successful consumption nor proof all dynamic entry paths are absent",
    "checks": checks,
    "product_evidence_added": False,
    "business_tests_rerun": False,
    "device_accessed": False,
}
(OUT / "zcode-backlog-audit.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({key: report[key] for key in ("source_commit", "committed_board", "workspace_board", "entry_domain_imports")}, ensure_ascii=False, indent=2))
print("zcode_owned", len(owned), counts(owned))
