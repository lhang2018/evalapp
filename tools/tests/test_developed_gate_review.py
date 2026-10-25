"""Invalid release metadata must never produce an affirmative summary."""
import copy
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import check_release_gates as gates

ROOT=Path(__file__).resolve().parents[2]

def test_empty_gate_and_range_lists_are_not_release_ready():
    invalid=dict(id="empty",frozen_range={"required":[]},gates=[],
                 first_task_chain={},delivery_package=["placeholder"])
    assert gates.validate(invalid, {})
    assert not gates.summarize(invalid,{})["release_ready"]

def test_missing_evidence_cannot_be_hidden_by_all_pass_statuses():
    table=json.loads(gates.DEFAULT_EVIDENCE.read_text(encoding="utf-8"))
    for item in table["gates"]+table["frozen_range"]["required"]:
        item["status"]="pass";item["evidence"]=[]
    board={"baseline":{"published_commit":"a"*40}}
    assert gates.validate(table,board)
    assert not gates.summarize(table,board)["release_ready"]
