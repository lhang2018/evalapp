"""Record final validation while retaining the failed first full run."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
prefix = folder.relative_to(root).as_posix()
feature = "b5b376d386d3aee58529007df30fc55d1386c687"
code_commit = "026ab2b7e2b63410d939d7c285f9ef8ac3f973ad"
commit = "67fcc54275b09b06232a67bd0cf1530da7e60d84"
client_commit = "998db6fd6427586a4ba23bd99bf91a7edf03625e"
report = "docs/协作开发/验收/2026-10-26-反馈私钥与模块身份复核.md"
evidence = prefix + "/committed-evidence.json"
names = ["negative-validation.json", "focused-validation.json", "focused-final-validation.json",
         "namespace-validation.json", "related-final-validation.json", "fixed-validation.json",
         "rejection-negative-validation.json", "rejection-fixed-validation.json", "final-fixed-validation.json",
         "ledger-fixed-validation.json"]
records = {name: json.loads((folder / name).read_text(encoding="utf-8")) for name in names}
fixed = records["ledger-fixed-validation.json"]
assert fixed["snapshot_commit"] == commit and fixed["exit_code"] == 0
assert {"tests/test_ledger_drift.py", "tools/tests"}.issubset(fixed["pytest_arguments"])
assert records["final-fixed-validation.json"]["snapshot_commit"] == code_commit
assert records["final-fixed-validation.json"]["exit_code"] == 1
assert records["fixed-validation.json"]["snapshot_commit"] == feature
assert records["fixed-validation.json"]["exit_code"] == 1
assert all(row["temporary_data_removed"] for row in records.values())
assert "1 failed, 2343 passed, 1 skipped, 1 deselected" in (folder / "full-backend-final.txt").read_text(encoding="utf-8")
assert "85 passed" in (folder / "ledger-fixed.txt").read_text(encoding="utf-8")
passed, skipped, deselected = 2344, 1, 1  # Deduplicated coverage after the single documentation failure is fixed.
def git(*args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
client_tree = git("rev-parse", commit + ":android-app")
assert client_tree == git("rev-parse", client_commit + ":android-app")
assert set(git("diff", "--name-only", code_commit, commit).splitlines()) == {"FEATURES.md", "IMPLEMENTATION.md"}
code_trees = {name: git("rev-parse", commit + ":" + name) for name in ("evoagent", "tests", "tools")}
assert all(value == git("rev-parse", code_commit + ":" + name) for name, value in code_trees.items())
logs = ["negative-probes.txt", "focused-tests.txt", "focused-final.txt", "namespace-probes.txt",
        "related-final.txt", "full-backend.txt", "rejection-negative.txt", "rejection-fixed.txt", "full-backend-final.txt", "ledger-fixed.txt"]
summary = dict(source_commit=commit, feature_fix_commit=feature, api_fix_commit=code_commit, source_tree=fixed["source_tree"],
    python_passed=passed, python_skipped=skipped, python_deselected=deselected,
    validation_mode="full runtime plus metadata revalidation with identical product/test/tool trees; no third full run",
    code_trees=code_trees, metadata_recheck_passed=85,
    second_full_result=dict(source_commit=code_commit, passed=2343, failed=1, failure="ledger reference drift; fixed by metadata-only commit"),
    records=records, initial_full_result=dict(passed=2341, failed=1, skipped=1, deselected=1,
        failure="test_diagnostics_import.py::TestDiagnosticsImport::test_content_type_and_admin_required; WinError10053",
        resolution="bounded drain on diagnostics rejection; deterministic regression, no test assertion relaxation"),
    client_passed=424, typecheck="passed", client_source_commit=client_commit, client_tree=client_tree,
    client_evidence_mode="reused_identical_client_tree", client_evidence=".planning/2026-10-26-device-review/committed-evidence.json",
    device_verified=False, product_acceptance_added=False, new_temporary_data_removed=True,
    evidence_sha256_lf={name: hashlib.sha256((folder / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for name in logs})
(folder / "committed-evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
next_steps = {
    "ALN-29-C": "b5b376d补私钥片段检测/零入库；026ab2b修诊断拒绝连接，67fcc54修台账锚点。代码全量2343通过，唯一文档失败修复后85项复检通过，去重覆盖2344/1/1；未第三次全套。客户端同树沿用424，新修复待复核，产品缺口保留。",
    "ALN-18-L": "b5b376d拒绝别名重绑/包导出遮蔽/命名空间歧义；合法项目应用结果保持。最终代码/台账分层验证覆盖2344/1/1（2343过+文档修复后85复检，无第三次全套）；仍非LSP，新修复待复核，真实项目/装配另验。",
}
for card in board["dispatches"]:
    if card["id"] not in next_steps:
        continue
    assert card["status"] == "review"
    card["next_step"] = next_steps[card["id"]]
    if not any(isinstance(row, dict) and row.get("log") == evidence for row in card["evidence"]):
        card["evidence"].append(dict(date="2026-10-26", source_commit=commit, functional_commit=feature,
            source_tree=fixed["source_tree"], scope="29-C私钥信封/18-L模块身份，按本卡范围；附带诊断早拒绝连接修复",
            result="代码2343通过、唯一文档失败修复后85项复检通过；去重覆盖2344/1/1，产品/测试/工具树相同，未第三次全套；两轮原失败保留；客户端沿用424，未独立接受/未发布",
            log=evidence, delivery=report))
for anchor, title, workpackage in [
    (feature, "fix(review): 拦截私钥片段并拒绝模块身份歧义改写", "ALN-29"),
    (code_commit, "fix(api): 在诊断导入拒绝前有界丢弃小请求体", "ALN-16"),
]:
    change_id = "discovered-" + anchor[:12]
    if not any(row["id"] == change_id for row in board["post_candidate_changes"]):
        board["post_candidate_changes"].append(dict(id=change_id, title=title, workpackage=workpackage,
            source_commit=anchor, status="source_registered_pending_validation",
            files=git("diff-tree", "--no-commit-id", "--name-only", "-r", anchor).splitlines(),
            candidate_included=False, independent_review=False, report=report,
            scope="反馈/重构及全量发现的诊断拒绝修复；最终2344/1/1，新修复待独立接受且未纳入发布基线"))
batch_id = "REVIEW-2026-10-26-FEEDBACK-REFACTOR"
if not any(row["id"] == batch_id for row in board["code_review_batches"]):
    board["code_review_batches"].append(dict(id=batch_id, date="2026-10-26", reviewer="codex-runtime",
        cards=list(next_steps), fixed_cards=list(next_steps), fix_commit=code_commit, feature_fix_commit=feature, validation_commit=commit,
        report=report, evidence=evidence, status="fixes_delivered_pending_independent_recheck",
        product_acceptance_added=False, published_by_review=False))
baseline = board["baseline"]
previous = baseline.get("latest_source_verification")
history = baseline.setdefault("source_verification_history", [])
if previous and previous.get("source_commit") != commit and previous not in history:
    history.append(previous)
baseline["latest_source_verification"] = dict(source_commit=commit, source_tree=fixed["source_tree"],
    python_passed=passed, python_skipped=skipped, python_deselected=deselected,
    client_passed=424, typecheck="passed", client_source_commit=client_commit,
    client_evidence_mode="reused_identical_client_tree", device_verified=False,
    validation_mode="identical-code metadata revalidation; no third full rerun",
    scope="026ab2b全量2343通过/1台账失败，67fcc54仅修引用后台账+工具85通过；产品/测试/工具树相同，去重覆盖2344/1/1；两轮原失败保留，无第三次全套；客户端沿用998db6f的424，非重跑；无真机/生产接受，不替换基线",
    evidence="../../" + evidence)
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
