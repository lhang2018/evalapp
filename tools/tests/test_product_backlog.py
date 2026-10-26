"""Prevent newly registered work from bypassing original contracts or ownership."""
import copy
import json
from pathlib import Path
import sys

import pytest

TOOLS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOLS))
import check_collaboration_plan as cp

REAL_ROOT=TOOLS.parent

@pytest.fixture
def board(tmp_path,monkeypatch):
    value=json.loads((REAL_ROOT/"docs/协作开发/workboard.json").read_text(encoding="utf-8"))
    # Ownership in the live checkout can change during other developers' work.
    # Test a stable example instead of coupling unit probes to their in-flight edits.
    for item in value["dispatches"]:
        if item.get("requirement_batch") != "PRODUCT-2026-10-25":
            continue
        own=item["id"] in {"ALN-29-A","ALN-23-A"}
        item.update(owner="codex-runtime" if own else None, reviewer=None,
                    branch="fixture-branch" if own else None, base_commit="a"*40 if own else None,
                    status="in_progress" if item["id"]=="ALN-29-A" else "claimed" if own else "unclaimed")
    value["execution_queue"]=dict(owner="codex-runtime",mode="test",wip_limit=1,
        active_task="ALN-29-A",next_task="ALN-23-A",items=[
            dict(id="ALN-29-A",state="in_progress",condition="test"),
            dict(id="ALN-23-A",state="claimed",condition="test")])
    # Use a self-contained metadata copy; no writes to the live shared workboard.
    for batch in value["requirement_batches"]:
        plan=json.loads((REAL_ROOT/batch["plan"]).read_text(encoding="utf-8"))
        p=tmp_path/batch["plan"];p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(plan,ensure_ascii=False),encoding="utf-8")
        for source in plan["sources"]:
            p=tmp_path/source;p.parent.mkdir(parents=True,exist_ok=True);p.touch()
    for item in value["dispatches"]:
        p=tmp_path/item["card"];p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(item["id"]+"\n## 目标\n## 写入边界\n## 验收\n## 交付\n",encoding="utf-8")
    for item in value["shared_files"]:
        p=tmp_path/item["path"];p.parent.mkdir(parents=True,exist_ok=True);p.touch()
    p=tmp_path/value["baseline"]["snapshot"];p.parent.mkdir(parents=True,exist_ok=True);p.touch()
    monkeypatch.setattr(cp,"ROOT",tmp_path)
    monkeypatch.setattr(cp,"RENDERED",tmp_path/"rendered.md")
    return value

def check(board):
    cp.RENDERED.write_text(cp.render(board),encoding="utf-8")
    return cp.validate(board)

def change_plan(board,fn):
    path=cp.ROOT/board["requirement_batches"][0]["plan"]
    plan=json.loads(path.read_text(encoding="utf-8"));fn(plan)
    path.write_text(json.dumps(plan,ensure_ascii=False),encoding="utf-8")

def test_registered_plan_preserves_originals_and_renders_dynamic_count(board):
    assert not check(board)
    assert f"{len(board['workpackages'])} 个工作包总账" in cp.render(board)

def test_original_package_cannot_be_removed(board):
    board["workpackages"]=[p for p in board["workpackages"] if p["id"]!="ALN-01"]
    assert check(board)

def test_original_dependencies_and_titles_remain_protected(board):
    board["workpackages"][1]["title"]="rewritten original contract"
    assert any("changed original" in e for e in check(board))

def test_unregistered_extensions_are_rejected(board):
    p=copy.deepcopy(board["workpackages"][-1]);p["id"]="ALN-99"
    board["workpackages"].append(p)
    assert check(board)

def test_missing_planned_card_is_rejected(board):
    board["dispatches"]=[d for d in board["dispatches"] if d["id"]!="ALN-25-A"]
    assert any("planned task not registered" in e for e in check(board))

def test_extension_cannot_change_registered_closure(board):
    board["workpackages"][-1]["closure_criteria"]="all green"
    assert any("extension contract" in e for e in check(board))

def test_task_dependency_cycles_are_rejected(board):
    next(d for d in board["dispatches"] if d["id"]=="ALN-29-A")["depends_on"]=["ALN-23-A"]
    assert any("cycle" in e for e in check(board))

def test_package_cycles_are_rejected_even_if_manifest_agrees(board):
    package=next(p for p in board["workpackages"] if p["id"]=="ALN-23")
    package["depends_on"].append("ALN-29")
    change_plan(board,lambda p:next(x for x in p["new_workpackages"] if x["id"]=="ALN-23")["depends_on"].append("ALN-29"))
    assert any("cycle" in e for e in check(board))

def test_all_28_ux_requirements_must_have_real_task_mappings(board):
    change_plan(board,lambda p:p["ux_coverage"]["UX-02"].clear())
    assert any("unmapped UX" in e for e in check(board))

def test_all_17_cases_must_be_mapped(board):
    change_plan(board,lambda p:p["acceptance_coverage"].pop("UX-F03-05"))
    assert any("17 acceptance" in e for e in check(board))

def test_same_owner_cannot_develop_two_cards(board):
    next(d for d in board["dispatches"] if d["id"]=="ALN-23-A")["status"]="in_progress"
    assert any("single in-progress" in e for e in check(board))

def test_new_claim_requires_exact_source_anchor(board):
    next(d for d in board["dispatches"] if d["id"]=="ALN-23-A")["base_commit"]="5e2b7e3"
    assert any("branch/base_commit" in e for e in check(board))

def test_plan_cannot_escape_repository(board):
    board["requirement_batches"][0]["plan"]="../outside.json"
    assert any("outside repository" in e for e in check(board))

def test_stale_rendered_board_is_rejected(board):
    cp.RENDERED.write_text("stale",encoding="utf-8")
    assert any("Markdown board is stale" in e for e in cp.validate(board))
