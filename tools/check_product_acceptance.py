"""Validate the first-release case matrix or check supplied recorded observations.

Default exit=0 means the matrix/evidence structure is valid, not that features pass.
Use --require-pass to fail if any of the 17 cases is failed or not_run.
"""
import argparse
import json
from pathlib import Path
import subprocess

from product_acceptance import evaluate

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=ROOT/"docs/协作开发/验收/首发验收矩阵.json")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--source-commit", help="exact tested source commit; default current HEAD")
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args()
    try:
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
        evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else {"schema_version":1,"runs":[]}
        commit = args.source_commit or subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
        resolved = subprocess.run(["git","cat-file","-e",commit+"^{commit}"],cwd=ROOT,capture_output=True)
        if resolved.returncode:
            raise ValueError("source commit is not a local Git commit")
        report = evaluate(matrix,evidence,ROOT,commit)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps({"matrix_valid":False,"errors":[str(exc)],"all_passed":False},ensure_ascii=False))
        return 1
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 1 if not report["matrix_valid"] or report["counts"]["failed"] or (args.require_pass and not report["all_passed"]) else 0

if __name__ == "__main__":
    raise SystemExit(main())
