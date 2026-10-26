"""Inventory acceptance provenance; missing conventional fields are warnings."""
from collections import Counter
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
source = subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
board = json.loads((root/"docs/协作开发/workboard.json").read_text(encoding="utf-8"))


def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


refs = set()
missing_files = []
cards = []
for card in board["dispatches"]:
    commits = set()
    reports = set()
    for key, value in walk(card):
        if not isinstance(value,str):
            continue
        if key.endswith("commit") and re.fullmatch(r"[0-9a-f]{7,40}", value):
            commits.add(value)
            refs.add(value)
        if (key in {"card","delivery","report","review_report","reviewed_report","acceptance_report"}
                and value.startswith("docs/") and value.endswith(".md")):
            reports.add(value)
            if not (root/value).is_file():
                missing_files.append(dict(card=card["id"],field=key,path=value))
    cards.append(dict(id=card["id"],status=card["status"],owner=card.get("owner"),
                      reviewer=card.get("reviewer"),delivered_commit=card.get("delivered_commit"),
                      commit_refs=sorted(commits),document_refs=sorted(reports)))
ordered = sorted(refs)
query = "".join(ref+"^{commit}\n" for ref in ordered)
result = subprocess.run(["git","cat-file","--batch-check"],input=query,cwd=root,
                         capture_output=True,text=True,encoding="utf-8",check=True)
resolution = {}
for ref, line in zip(ordered,result.stdout.splitlines()):
    fields = line.split()
    resolution[ref] = fields[0] if len(fields)==3 and fields[1]=="commit" else None
unresolved = [{"card":card["id"],"ref":ref} for card in cards
              for ref in card["commit_refs"] if resolution.get(ref) is None]
accepted = [card for card in cards if card["status"]=="accepted"]
warnings = [{"card":card["id"],"kind":"missing_standard_delivered_commit",
             "note":"Historical evidence may contain source anchors; requires manual reading, not an acceptance failure"}
            for card in accepted if not card["delivered_commit"]]
report = dict(source_commit=source,scope="105-card metadata/provenance inventory; not 75-card functional acceptance",
              counts=dict(Counter(card["status"] for card in cards)),cards=len(cards),
              accepted_count=len(accepted),missing_document_refs=missing_files,
              unresolved_commit_refs=unresolved,conventional_field_warnings=warnings,
              commit_resolution=resolution,inventory=cards)
(folder/"ledger-inventory.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps({key:report[key] for key in ("cards","counts","missing_document_refs","unresolved_commit_refs")},ensure_ascii=False))
print("Accepted cards without conventional delivered_commit:",len(warnings),"(warnings, not failed acceptance)")
