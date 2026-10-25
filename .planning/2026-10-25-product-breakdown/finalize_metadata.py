"""Register observed committed source deltas without accepting another author's work."""
import json
from pathlib import Path
import re
import subprocess
import sys

root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/"tools"))
from check_source_coverage import check
head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
report=check(root,head)
groups={}
for path in report["unregistered_files"]:
    commit=subprocess.check_output(["git","log","-1","--format=%H",head,"--",path],cwd=root,text=True).strip()
    groups.setdefault(commit,[]).append(path)
board_path=root/"docs/协作开发/workboard.json"
board=json.loads(board_path.read_text(encoding="utf-8"))
for commit,files in groups.items():
    identity="discovered-"+commit[:12]
    if any(e["id"]==identity for e in board.get("post_candidate_changes",[])):
        continue
    subject=subprocess.check_output(["git","show","-s","--format=%s",commit],cwd=root,text=True,encoding="utf-8").strip()
    parent=re.search(r"ALN-\d{2}",subject)
    board.setdefault("post_candidate_changes",[]).append(dict(
        id=identity,title=subject,workpackage=parent.group(0) if parent else "ALN-00",
        source_commit=commit,status="source_registered_pending_validation",files=files,
        candidate_included=False,independent_review=False,
        report="docs/协作开发/需求拆分与分期实施_2026-10-25.md",
        scope="仅按Git对象发现并登记这些提交路径；不替原作者宣布卡片完成、审阅接受、真机通过或纳入候选"))
board_path.write_text(json.dumps(board,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
print(json.dumps(dict(registered_source_commits=list(groups),files=sum(map(len,groups.values()))),ensure_ascii=False))
