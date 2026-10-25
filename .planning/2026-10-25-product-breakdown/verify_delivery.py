"""Capture a bounded verification snapshot while other developers keep working."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

root=Path(__file__).resolve().parents[2]
out=Path(__file__).resolve().parent
head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
commands=[
    ("tool-tests",["-m","pytest","tools/tests/test_product_acceptance.py","tools/tests/test_product_backlog.py","-q"],0),
    ("matrix",["tools/check_product_acceptance.py","--source-commit",head],0),
    ("matrix-require-pass",["tools/check_product_acceptance.py","--source-commit",head,"--require-pass"],1),
    ("collaboration",["tools/check_collaboration_plan.py"],0),
    ("prepared",["tools/check_collaboration_plan.py","--verify-prepared"],0),
    ("source-coverage",["tools/check_source_coverage.py","--head",head],0),
    ("original-design",[".planning/2026-10-22-design-alignment/verify_alignment_plan.py"],0),
    ("claims",["tools/verify_claims.py","--check"],0),
    ("ledger",["tools/scan_all_ledgers.py"],0),
]
results=[]
for name,args,expected in commands:
    result=subprocess.run([sys.executable,"-X","utf8",*args],cwd=root,capture_output=True,text=True,encoding="utf-8")
    log=out/(name+".txt")
    log.write_text(result.stdout+result.stderr,encoding="utf-8",newline="\n")
    results.append(dict(name=name,command=[sys.executable,"-X","utf8",*args],returncode=result.returncode,
        expected_returncode=expected,ok=result.returncode==expected,log=log.relative_to(root).as_posix()))
    if name=="matrix":
        matrix=json.loads(result.stdout)
        assert matrix["counts"]=={"passed":0,"failed":0,"not_run":17} and not matrix["all_passed"]
files=["tools/check_collaboration_plan.py","tools/product_acceptance.py","tools/check_product_acceptance.py",
       "tools/tests/test_product_acceptance.py","tools/tests/test_product_backlog.py",
       "docs/协作开发/验收/首发验收矩阵.json"]
hashes=[dict(path=f,sha256_lf=hashlib.sha256((root/f).read_bytes().replace(b"\r\n",b"\n")).hexdigest()) for f in files]
links,missing=0,[]
for relative in ["docs/协作开发/需求拆分与分期实施_2026-10-25.md",
                 "docs/协作开发/交付/ALN-29-A-首发验收骨架与断言.md"]:
    path=root/relative
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)",path.read_text(encoding="utf-8")):
        if target.startswith(("http:","https:","#")):continue
        links+=1
        resolved=path.parent/unquote(target.split("#")[0])
        # Written immediately after the source commit is pinned.
        if resolved.name=="committed-evidence.json":continue
        if not resolved.is_file():missing.append(f"{relative}: {target}")
summary=dict(verification_anchor=head,scope="current working tree tool changes; concurrent source work excluded from this card",
    checks=results,source_files=hashes,local_links=links,missing_links=missing,
    business_tests_rerun=False,device_tests_rerun=False,product_cases_not_run=17,
    ok=all(r["ok"] for r in results) and not missing)
(out/"verification.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
print(json.dumps({"head":head,"checks":{r["name"]:r["ok"] for r in results},
                  "missing_links":missing,"ok":summary["ok"]},ensure_ascii=False))
raise SystemExit(0 if summary["ok"] else 1)
