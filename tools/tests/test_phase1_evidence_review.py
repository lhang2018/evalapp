"""A passing component process must never become a passing product scenario."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import product_acceptance
from record_phase1_evidence import collect_component_probes

ROOT=Path(__file__).resolve().parents[2]

@pytest.mark.parametrize("exitcode",[0,5])
def test_component_result_preserves_exit_status_but_no_product_runs(tmp_path,exitcode):
    out=tmp_path/"component.json"
    bundle=collect_component_probes(tmp_path,out,[
        ("synthetic-test",[sys.executable,"-c",f"print('component only'); raise SystemExit({exitcode})"])])
    assert json.loads(out.read_text(encoding="utf-8")) == bundle
    assert bundle["runs"]==[]
    assert bundle["source_scope"]=="working_tree"
    result=bundle["component_results"][0]
    assert result["returncode"]==exitcode and result["ok"] is (exitcode==0)
    artifact=Path(result["artifact"]["path"])
    if not artifact.is_absolute():artifact=tmp_path/artifact
    assert hashlib.sha256(artifact.read_bytes()).hexdigest()==result["artifact"]["sha256"]

def test_withdrawn_phase1_claims_cannot_count_as_8_product_passes():
    evidence=json.loads((ROOT/".planning/2026-10-25-product-breakdown/evidence-phase1-withdrawal.json").read_text(encoding="utf-8"))
    matrix=json.loads((ROOT/"docs/协作开发/验收/首发验收矩阵.json").read_text(encoding="utf-8"))
    result=product_acceptance.evaluate(matrix,evidence,ROOT,"a"*40)
    assert result["counts"]=={"passed":0,"failed":0,"not_run":17}
    assert not result["all_passed"]
    assert len(evidence["quarantined_records"])==8
