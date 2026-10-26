"""Check provenance and unchanged assignments against the fixed pre-split board."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
source = "0e1b4fd61c9f616f9f9fe560b39f3d759d7437cd"
board_path = "docs/协作开发/workboard.json"
plan_path = "docs/协作开发/需求拆分与分期实施_2026-10-25.json"
original = json.loads(subprocess.check_output(["git", "show", source+":"+board_path],cwd=root).decode("utf-8"))
board = json.loads((root/board_path).read_text(encoding="utf-8"))
plan = json.loads((root/plan_path).read_text(encoding="utf-8"))
old = {row["id"]:row for row in original["dispatches"]}
current = {row["id"]:row for row in board["dispatches"]}
new = [row for row in board["dispatches"] if row["id"] not in old]
assert len(new) == 17 and len(current) == 105
for identity, row in old.items():
    assert all(current[identity].get(field) == row.get(field) for field in ("owner", "reviewer", "status"))
for row in new:
    assert row["status"] == "unclaimed"
    assert all(row.get(field) is None for field in ("owner", "reviewer", "branch", "base_commit"))
    assert row["depends_on"] and all(dep in current for dep in row["depends_on"])
    assert row["split_round"] == "STATUS-SPLIT-2026-10-26"
    task = next(task for task in plan["tasks"] if task["id"] == row["id"])
    assert task["goal"] and len(task["acceptance"].split("；")) >= 3
    text = (root/row["card"]).read_text(encoding="utf-8")
    assert all(heading in text for heading in ("## 目标", "## 写入边界", "## 验收", "## 交付"))
for row in original["workpackages"]:
    current_package = next(package for package in board["workpackages"] if package["id"] == row["id"])
    for field in ("title", "depends_on", "closure_criteria", "source_refs"):
        assert current_package.get(field) == row.get(field)
records = {name:json.loads((folder/name).read_text(encoding="utf-8")) for name in ("runtime-validation.json","collection-validation.json","tools-validation.json")}
assert all(row["exit_code"] == 0 and row["temporary_data_removed"] for row in records.values())
source_tree = subprocess.check_output(["git","rev-parse",source+"^{tree}"],cwd=root,text=True).strip()
assert all(records[name]["source_tree"] == source_tree for name in ("runtime-validation.json","collection-validation.json"))
assert "176 passed" in (folder/"runtime.txt").read_text(encoding="utf-8")
assert "2328 tests collected" in (folder/"collection.txt").read_text(encoding="utf-8")
assert "70 passed" in (folder/"tools.txt").read_text(encoding="utf-8")
digest = lambda path: hashlib.sha256(path.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
summary_path = folder/"split-summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
summary["source_sha256_lf"] = {name:digest(root/name) for name in (board_path,plan_path)}
summary_path.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
result = dict(source_commit=source, original_cards_preserved=len(old), original_packages_preserved=len(original["workpackages"]),
              new_unclaimed_cards=len(new), runtime_passed=176, collected_only=2328,
              updated_metadata_tools_passed=70, source_tree=source_tree,
              full_backend_rerun=False, product_acceptance_added=False, records=records,
              all_new_temporary_data_removed=True,
              log_sha256_lf={name:digest(folder/name) for name in ("runtime.txt","collection.txt","tools.txt")})
(folder/"status-evidence.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("Original 88 assignments/statuses and 31 package contracts preserved; 17 unclaimed cards verified")
