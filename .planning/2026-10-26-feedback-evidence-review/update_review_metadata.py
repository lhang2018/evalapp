"""Register fixed-source review evidence without changing ownership or acceptance."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
fix = "1c2e2052b4f2d33103ceb257e02e5be3bbafc57f"
report = "docs/协作开发/验收/2026-10-26-反馈聚合与审核证据复核.md"
evidence_path = ".planning/2026-10-26-feedback-evidence-review/committed-evidence.json"
records = {}
for name in ("negative-validation", "focused-validation", "focused-2-validation",
             "http-validation", "committed-validation", "command-id-matrix"):
    value = json.loads((folder / (name + ".json")).read_text(encoding="utf-8"))
    owned = Path(value["owned_temp_directory"])
    assert owned.resolve().parent == (root / "outputs").resolve()
    assert value["temporary_data_removed"] and not owned.exists()
    records[name] = value
assert records["committed-validation"]["snapshot_commit"] == fix
assert records["committed-validation"]["exit_code"] == 0
assert records["command-id-matrix"]["all_expected"]
record = {
    "date": "2026-10-26", "reviewer": "codex-runtime", "cards": ["ALN-29-C"],
    "fix_commit": fix, "source_tree": records["committed-validation"]["source_tree"],
    "report": report, "environment": {"platform": platform.platform(), "python": sys.version},
    "results": {"new_initial_probes": "17 failed, 1 passed",
                "focused": "41 passed, 6 deselected", "live_http_permission": "1 passed, 20 deselected",
                "fixed_related_backend": "338 passed, 1 deselected",
                "new_probes_in_fixed_run": 21,
                "id_length_matrix": "74 chars: 1 failed/12 deselected; 64 chars: 1 passed/12 deselected"},
    "validation_records": records, "owned_validation_scopes_removed": len(records),
    "not_run": ["whole-host CPU saturation", "full backend", "fresh client/TS", "Kotlin compile",
                "new APK/device", "real users/provider"],
    "historical_review_correction": {"report_commit": "ec014c9", "original_logs_preserved": True,
        "all_runs_source_fixed": False, "cause_claim": "length variant deterministic; old per-run source not reconstructed"},
    "status": "fixes_delivered_pending_independent_recheck", "old_caches_and_foreign_changes_preserved": True,
    "log_sha256_lf": {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                      for p in folder.glob("*.txt")},
}
(folder / "committed-evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
board_file = root / "docs/协作开发/workboard.json"
board = json.loads(board_file.read_text(encoding="utf-8"))
card = next(c for c in board["dispatches"] if c["id"] == "ALN-29-C")
assert card["status"] == "review" and card["owner"] == "zcode-main"
card["blocker"] = "1c2e205新修复（聚合证据/时间/坏清单）待另一审阅人复核；原作者不自批，整包不关闭"
card["next_step"] = "固定1c2e205相关338通过/1整机压力未跑；复核证据合并32上限/并发/回滚、真实HTTP权限与旧记录损坏拒绝；事件来源验真/授权证据和产品闭环沿用原设计；" + report
entry = {"date": "2026-10-26", "reviewer": "codex-runtime", "source_commit": fix,
         "source_tree": record["source_tree"], "report": report, "log": evidence_path,
         "scope": "聚合引用原子并集/32上限、时间有限值和时钟回退、坏引用/运行清单拒绝；真实HTTP权限/事务回滚；21新增探针",
         "result": "固定相关338 passed/1 deselected；非全量/客户端/设备/生产接受；review保持，新补丁待另一审阅人复核"}
if entry not in card["evidence"]:
    card["evidence"].append(entry)
card25 = next(c for c in board["dispatches"] if c["id"] == "ALN-25-C")
first = card25.get("first_review", {})
if "偶发" in first.get("finding", ""):
    first.setdefault("historical_finding", first["finding"])
    first["finding"] = "F1归因已更正：固定两变体证明74字符命令键确定性失败/64通过；旧各组源码未固定，不再要求无依据改测试隔离"
    first["correction_report"] = report
entry25 = {"date": "2026-10-26", "reviewer": "codex-runtime", "source_commit": fix,
           "report": report, "log": evidence_path,
           "scope": "更正旧F1偶发归因：6e82856固定归档仅修改键前缀的两变体；旧日志每组源码未固定，原失败保留",
           "result": "74字符必失败/64字符通过，当前键修复有效；固定1c2e205相关338亦含App用例，既有接受阻塞不变"}
if entry25 not in card25["evidence"]:
    card25["evidence"].append(entry25)
files = subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", fix],
                                cwd=root, text=True, encoding="utf-8").splitlines()
source = {"id": "review-fix-" + fix[:12], "title": "fix(feedback): 原子合并聚合证据并校验时间与运行清单",
          "workpackage": "ALN-29", "source_commit": fix, "files": files,
          "status": "source_registered_pending_validation", "candidate_included": False,
          "independent_review": False, "report": report,
          "scope": "29-C反馈证据/时间/清单边界，固定相关338/1项整机饱和未跑；新修复待独立复核"}
if not any(e["id"] == source["id"] for e in board["post_candidate_changes"]):
    board["post_candidate_changes"].append(source)
batch = {"id": "REVIEW-2026-10-26-FEEDBACK-EVIDENCE", "date": "2026-10-26", "reviewer": "codex-runtime",
         "cards": ["ALN-29-C", "ALN-25-C", "ALN-26-B"], "fixed_cards": ["ALN-29-C"],
         "fix_commit": fix, "report": report, "evidence": evidence_path,
         "status": "fixes_delivered_pending_independent_recheck", "published_by_review": False,
         "product_acceptance_added": False,
         "scope": "反馈四类缺陷修复，338固定相关；F1确定性长度/源码边界更正，26-B组件抽查不授予接受"}
if not any(e["id"] == batch["id"] for e in board["code_review_batches"]):
    board["code_review_batches"].append(batch)
board_file.write_text(json.dumps(board, ensure_ascii=False, indent=1)+"\n", encoding="utf-8")
print("registered fixed evidence; removed validation scopes:", len(records))
