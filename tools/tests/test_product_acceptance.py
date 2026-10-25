"""Negative probes for the acceptance harness. All records here are synthetic."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOLS))
import product_acceptance as pa

ROOT = TOOLS.parent
COMMIT = "a"*40

@pytest.fixture
def sample(tmp_path):
    matrix=json.loads((ROOT/"docs/协作开发/验收/首发验收矩阵.json").read_text(encoding="utf-8"))
    source=tmp_path/pa.SOURCE
    source.parent.mkdir(parents=True)
    source.write_bytes((ROOT/pa.SOURCE).read_bytes())
    trace=tmp_path/"synthetic-test-trace.txt"
    trace.write_text("SYNTHETIC unit-test observations; not product execution evidence.",encoding="utf-8")
    def put(observations,path,value):
        parts=path.split(".")
        for key in parts[:-1]:
            observations=observations.setdefault(key,{})
        observations[parts[-1]]=value
    records=[]
    for identity,predicates in pa.ASSERTIONS.items():
        obs={}
        for path,op,value in predicates:
            if op=="eq_path":
                put(obs,value,"manifest-test")
                value="manifest-test"
            elif op=="lte_path":
                put(obs,value,2)
                value=1
            put(obs,path,value)
        records.append(dict(case_id=identity,source_commit=COMMIT,
            environment=dict(kind="integration",run_id="synthetic-unit-test",description="Not real product evidence"),
            artifacts=[dict(path=trace.name,sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),kind="execution_log")],
            observations=obs))
    return tmp_path,matrix,dict(schema_version=1,runs=records)

def run(sample):
    root,matrix,evidence=sample
    return pa.evaluate(matrix,evidence,root,COMMIT)

def test_missing_evidence_stays_not_run(sample):
    sample[2]["runs"]=[]
    result=run(sample)
    assert result["matrix_valid"] and not result["all_passed"]
    assert result["counts"]==dict(passed=0,failed=0,not_run=17)

def test_all_assertions_can_evaluate_synthetic_observations(sample):
    # Tests the assertion engine; never saves this as a product acceptance report.
    assert run(sample)["all_passed"]

@pytest.mark.parametrize("identity",sorted(pa.EXPECTED_IDS))
def test_every_case_fails_on_a_missing_required_observation(sample,identity):
    record=next(r for r in sample[2]["runs"] if r["case_id"]==identity)
    path=pa.ASSERTIONS[identity][0][0].split(".")
    record["observations"].pop(path[0])
    result=run(sample)
    assert not result["all_passed"]
    assert next(c for c in result["cases"] if c["id"]==identity)["status"]=="failed"

@pytest.mark.parametrize("mutation",["missing_case","duplicate_case","weakened_assertion","changed_source","boolean_version","preaccepted"])
def test_matrix_cannot_lose_or_weaken_original_requirements(sample,mutation):
    matrix=sample[1]
    if mutation=="missing_case":matrix["cases"].pop()
    if mutation=="duplicate_case":matrix["cases"][-1]=copy.deepcopy(matrix["cases"][0])
    if mutation=="weakened_assertion":matrix["cases"][0]["assertions"]=[]
    if mutation=="changed_source":matrix["source"]["sha256_lf"]="0"*64
    if mutation=="boolean_version":matrix["schema_version"]=True
    if mutation=="preaccepted":matrix["cases"][0]["initial_status"]="passed"
    assert not run(sample)["matrix_valid"]

@pytest.mark.parametrize("mutation",["wrong_commit","no_artifact","changed_artifact","outside_path","symlink_escape","fixture","no_run_id"])
def test_false_or_unbound_evidence_cannot_pass(sample,mutation,tmp_path):
    record=sample[2]["runs"][0]
    if mutation=="wrong_commit":record["source_commit"]="b"*40
    if mutation=="no_artifact":record["artifacts"]=[]
    if mutation=="changed_artifact":record["artifacts"][0]["sha256"]="0"*64
    if mutation=="outside_path":record["artifacts"][0]["path"]="../outside.txt"
    if mutation=="symlink_escape":
        outside=tmp_path.parent/"outside-test-trace.txt"
        outside.write_text("outside",encoding="utf-8")
        link=tmp_path/"outside-link"
        try:link.symlink_to(outside)
        except OSError:pytest.skip("symlink creation unavailable")
        record["artifacts"][0].update(path=link.name,sha256=hashlib.sha256(outside.read_bytes()).hexdigest())
    if mutation=="fixture":record["environment"]["kind"]="fixture"
    if mutation=="no_run_id":record["environment"].pop("run_id")
    assert not run(sample)["all_passed"]
    assert run(sample)["counts"]["failed"]>=1

@pytest.mark.parametrize("record",[None,{"case_id":[]},{"case_id":"UNKNOWN"}])
def test_malformed_or_unknown_records_are_rejected(sample,record):
    sample[2]["runs"].append(record)
    assert not run(sample)["matrix_valid"]

def test_duplicate_records_do_not_overwrite_a_failure(sample):
    duplicate=copy.deepcopy(sample[2]["runs"][0])
    sample[2]["runs"][0]["observations"]={}
    sample[2]["runs"].append(duplicate)
    assert not run(sample)["matrix_valid"]
    assert not run(sample)["all_passed"]

@pytest.mark.parametrize("actual",[1,"true",None])
def test_bool_observations_are_not_truthiness_coerced(actual):
    assert not pa.compare({"ok":actual},("ok","eq",True))

@pytest.mark.parametrize("value",[float("nan"),float("inf"),True,-1])
def test_numeric_measurements_require_finite_nonnegative_numbers(value):
    assert not pa.compare({"n":value},("n","gte",1))

@pytest.mark.parametrize("value",["",None,{},[]])
def test_manifest_binding_must_be_a_nonempty_identifier(value):
    assert not pa.compare({"a":value,"b":value},("a","eq_path","b"))

def test_recovery_requires_a_positive_explicit_limit():
    assert not pa.compare({"attempts":0,"limit":0},("attempts","lte_path","limit"))

def test_cli_structure_is_not_a_claim_of_product_success():
    result=subprocess.run([sys.executable,"-X","utf8",str(TOOLS/"check_product_acceptance.py")],
        cwd=ROOT,capture_output=True,text=True,encoding="utf-8")
    assert result.returncode==0
    report=json.loads(result.stdout)
    assert report["counts"]["not_run"]==17 and not report["all_passed"]

def test_cli_require_pass_rejects_unexecuted_cases():
    result=subprocess.run([sys.executable,"-X","utf8",str(TOOLS/"check_product_acceptance.py"),"--require-pass"],
        cwd=ROOT,capture_output=True,text=True,encoding="utf-8")
    assert result.returncode==1 and not json.loads(result.stdout)["all_passed"]

def test_cli_rejects_supplied_failed_observations(tmp_path):
    import subprocess
    commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    path=tmp_path/"failed.json"
    path.write_text(json.dumps(dict(schema_version=1,runs=[
        dict(case_id="UX-F01-01",source_commit=commit,environment={"kind":"fixture"},
             observations={"answer":{"present":False}},artifacts=[])])),encoding="utf-8")
    result=subprocess.run([sys.executable,"-X","utf8",str(TOOLS/"check_product_acceptance.py"),"--evidence",str(path)],
        cwd=ROOT,capture_output=True,text=True,encoding="utf-8")
    assert result.returncode==1 and json.loads(result.stdout)["counts"]["failed"]==1
