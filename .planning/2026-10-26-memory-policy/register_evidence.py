"""Register bounded verification without rewriting full-suite or acceptance claims."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PREFIX = OUT.relative_to(ROOT).as_posix()
REPORT = "docs/协作开发/验收/2026-10-26-记忆范围策略主链复核.md"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


code = git("rev-parse", "c6c9872").decode().strip()
tested = "8267d6438a9a6adb517af24a37178bbb8e17c8a0"
records = {name: json.loads((OUT / name).read_text(encoding="utf-8"))
           for name in ("negative-validation.json", "unit-validation.json",
                        "fixed-validation.json", "final-validation.json")}
final = records["final-validation.json"]
assert final["exit_code"] == 0
assert all(value["temporary_data_removed"] for value in records.values())
# The runner was called with HEAD. Verify the actual archived bytes and tree
# against an immutable commit instead of rewriting its original invocation.
archive_digest = hashlib.sha256(git("archive", "--format=zip", tested)).hexdigest()
assert final["archive_sha256"] == archive_digest
assert final["source_tree"] == git("rev-parse", tested + "^{tree}").decode().strip()
assert "209 passed" in (OUT / "final.txt").read_text(encoding="utf-8")
assert "1 failed, 208 passed" in (OUT / "fixed.txt").read_text(encoding="utf-8")
assert git("rev-parse", code + ":evoagent") == git("rev-parse", tested + ":evoagent")
evidence = PREFIX + "/committed-evidence.json"
summary = dict(source_commit=tested, code_commit=code, source_tree=final["source_tree"],
               archive_sha256=archive_digest, resolved_archive_commit_verified=True,
               scope="memory, context/identity/runtime, ledger and tool tests; not full backend",
               passed=209, failed=0, full_backend_rerun=False, client_rerun=False,
               device_verified=False, product_acceptance_added=False,
               initial_failure="208 passed; sole hardcoded 30-package rendering assertion failed",
               initial_failure_resolution="test expects actual workpackage count; production renderer unchanged",
               records=records, new_temporary_data_removed=True,
               log_sha256_lf={name: hashlib.sha256((OUT/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest()
                              for name in ("negative.txt", "unit.txt", "fixed.txt", "final.txt")})
(OUT / "committed-evidence.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
path = ROOT / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
card = next(row for row in board["dispatches"] if row["id"] == "ALN-24-D")
assert card["status"] == "accepted"
if not any(isinstance(row,dict) and row.get("log") == evidence for row in card["evidence"]):
    card["evidence"].append(dict(date="2026-10-26",source_commit=tested,functional_commit=code,
        result="持久范围策略接真实检索，HTTP保存→模型消息/持久manifest排除禁用项；209项定向通过，非全量；新增修复待复核，原切片接受不扩大",
        log=evidence,delivery=REPORT))
card["next_step"] = "原控制面切片已接受；c6c9872新增持久范围策略检索约束，8267d64固定记忆/运行身份/台账工具209过，新增修复待复核。App界面、逐用户策略、备份/跨端擦除仍未闭合；不宣称整包完成。"
change_id = "discovered-" + code[:12]
if not any(row["id"] == change_id for row in board["post_candidate_changes"]):
    board["post_candidate_changes"].append(dict(id=change_id, title="fix(memory): 让持久范围策略约束真实检索与上下文",
        workpackage="ALN-24",source_commit=code,status="source_registered_pending_validation",
        files=git("diff-tree","--no-commit-id","--name-only","-r",code).decode().splitlines(),
        candidate_included=False,independent_review=False,report=REPORT,
        scope="定向209过，真实本机HTTP/控制器路径，非全量/产品接受/基线发布；新增修复待复核"))
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")
print(json.dumps({"source_commit":tested,"code_commit":code,"passed":209,"archive_verified":True},ensure_ascii=False))
