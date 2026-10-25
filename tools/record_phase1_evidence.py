"""Collect component-test logs, without claiming product end-to-end acceptance.

The original collector hard-coded observations and mislabeled simulated/historical
records. This replacement records actual process results only. Product runs stay
empty until scenario-specific end-to-end collectors exist and are reviewed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]

def collect_component_probes(root, out, commands):
    root, out = Path(root), Path(out)
    artifacts = out.parent / "component-probe-logs"
    artifacts.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
        capture_output=True, text=True).stdout.strip() or None
    results = []
    for name, command in commands:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        completed = subprocess.run(command, cwd=root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=env)
        log = artifacts / (name + "-" + uuid.uuid4().hex[:12] + ".txt")
        log.write_text((completed.stdout + completed.stderr).rstrip() + "\n",
                       encoding="utf-8", newline="\n")
        results.append({"name": name, "returncode": completed.returncode,
            "ok": completed.returncode == 0,
            "artifact": {"path": log.relative_to(root).as_posix()
                if log.is_relative_to(root) else str(log),
                "sha256": hashlib.sha256(log.read_bytes()).hexdigest()}})
    bundle = {"schema_version": 1, "runs": [],
        "scope": "component probes only; no product acceptance cases executed",
        "source_scope": "working_tree", "associated_head": head,
        "component_results": results}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    return bundle

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    commands = [
        ("python-components", [sys.executable, "-X", "utf8", "-m", "pytest",
          "tests/test_feedback.py", "tests/test_capability_status.py",
          "tests/test_asset_consumption.py", "-q"]),
        ("client-components", ["node", "--test",
          "android-app/test/offline/modes.test.ts",
          "android-app/test/offline/taskview.test.ts",
          "android-app/test/offline/capabilities.test.ts"]),
    ]
    bundle = collect_component_probes(ROOT, args.out.resolve(), commands)
    print(json.dumps({"component_suites": len(bundle["component_results"]),
        "product_cases_passed": 0, "product_cases_not_run": 17,
        "out": str(args.out)}, ensure_ascii=False))
    return 0 if all(r["ok"] for r in bundle["component_results"]) else 1

if __name__ == "__main__":
    raise SystemExit(main())
