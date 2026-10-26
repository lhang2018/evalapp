"""Persist per-card technical conclusions, preserving independent acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
fix = "f973dcc1fc68ab14f1c0d7d706718dc79ed5b037"
first_fix = "a435ce14ad0ef175a73fa1a8b628fbde31c047b6"
report = "docs/协作开发/验收/2026-10-26-指派十三项技术复核.md"
evidence_path = ".planning/2026-10-26-assigned-review/committed-evidence.json"
coverage = json.loads((folder / "coverage.json").read_text(encoding="utf-8"))
assert coverage["commit"] == fix
cases = ET.parse(folder / "junit.xml").findall(".//testcase")
assert len(cases) == 522 and not any(c.find("failure") is not None or c.find("error") is not None for c in cases)
records = {}
for name in ("negative-validation", "negative-2-validation", "focused-validation", "restore-validation",
             "initial-committed-validation", "validation", "guards-validation", "guard-negative-validation", "committed-validation"):
    value = json.loads((folder / (name + ".json")).read_text(encoding="utf-8"))
    owned = Path(value["owned_temp_directory"])
    assert owned.resolve().parent == (root / "outputs").resolve()
    assert value["temporary_data_removed"] and not owned.exists()
    records[name] = value
assert records["committed-validation"]["snapshot_commit"] == fix
assert records["committed-validation"]["exit_code"] == 0
assert not any(records["validation"]["results"].values())
def git(*args):
    return subprocess.check_output(["git", *args], cwd=root, text=True, encoding="utf-8").strip()
client_tree = git("rev-parse", fix + ":android-app")
assert client_tree == git("rev-parse", records["validation"]["commit"] + ":android-app")
gaps = {
 "ALN-30-B": "项目/工作区节点、全量文件下载、固定APK真机与后续模型实际禁用验证未闭合",
 "ALN-24-F": "真实API→模型消息/manifest的主体分区与Grant/撤销联验、App管理消费未闭合",
 "ALN-16-Q": "跨主体真实身份/授权证据消费、大导出/清理期和管理端设备仍待；跨库删除仅维护窗口，不能宣称全域擦除",
 "ALN-13-S": "本轮新调度补丁待独立复核；默认App真实资源/主动预算配置、运行中unknown/停止及生产长跑/多进程证据未闭合",
 "ALN-09-I": "candidate_id/dataset到权威仓库精确绑定、生产并发及完整管理入口未闭合",
 "ALN-18-H": "真实任务自动选专家→受信根安装→失败回滚再检闭环、真实项目/升级恢复仍待",
 "ALN-19-A": "真实供应商可播放产物/协议/价格与账单、持久预算权威未提供，现仅本地HTTP夹具及单实例费用状态",
 "ALN-27-C": "正式验收项2用户/项目可见性绑定仍缺；真实同任务集前后质量/退化、多进程及逆向迁移仍待",
 "ALN-15-E": "默认HeadlessTask仍未注入sampler/highWater；策略下发、Kotlin重编译/新APK真机周期压力及OEM不可读处理待补",
 "ALN-25-C": "默认宿主真实sampler/预算账户、实际用量对账、权威并发/运行中pause-stop和真实核心质量/延迟仍待",
 "ALN-30-A": "本地真实部署三库恢复成功链路已补证；原作者claude-main不能终审，需另分配独立终审，旧codex补丁也需独立确认",
 "ALN-25-B": "本轮未重造真实CPU饱和；旧0.768仅新准入；5bc1725补丁独立复核、真实执行层/固定设备非干扰仍待",
 "ALN-18-L": "仍为受限AST/符号分析，语义LSP和真实专家装配未实现；旧codex修复需独立复核",
}
board_file = root / "docs/协作开发/workboard.json"
board = json.loads(board_file.read_text(encoding="utf-8"))
by_id = {c["id"]: c for c in board["dispatches"]}
conclusions = {}
for identity, modules in coverage["cards"].items():
    selected = [c for c in cases if set(c.attrib["classname"].split(".")) & set(modules)]
    checks = {"backend_passed": len(selected), "backend_modules": modules}
    if identity in coverage["client_cards"]:
        checks.update(client_passed=459, typecheck_exit=0, client_commit=records["validation"]["commit"], client_tree=client_tree)
    result = {
        "date": "2026-10-26", "reviewer": "codex-runtime", "reviewed_source_commit": fix,
        "source_tree": records["committed-validation"]["source_tree"], "report": report, "evidence": evidence_path,
        "status": "changes_fixed_pending_independent_recheck" if identity == "ALN-13-S" else "technical_recheck_complete_with_acceptance_gaps",
        "checks": checks, "remaining_acceptance_gaps": gaps[identity],
        "independent_acceptance_added": False, "prior_codex_fixes_require_independent_review": True,
    }
    card = by_id[identity]
    card["technical_review"] = result
    card["next_step"] = "codex技术复核已完成（固定f973dcc相关522/客户端同一子树459/tsc0，逐卡范围见报告）；" + gaps[identity] + "；" + report
    card["blocker"] = gaps[identity]
    entry = {"date": "2026-10-26", "reviewer": "codex-runtime", "source_commit": fix,
             "report": report, "log": evidence_path, "scope": "指派卡技术复核；本卡后端相关"+str(len(selected))+"项（模块交叉计数不可相加）",
             "result": result["status"] + "；" + gaps[identity]}
    if entry not in card["evidence"]:
        card["evidence"].append(entry)
    conclusions[identity] = result
card = by_id["ALN-30-A"]
if card["status"] == "accepted":
    card.setdefault("review_history", []).append({"date": "2026-10-26", "previous_status": "accepted",
        "previous_reviewer": card.get("reviewer"), "reason": "原accepted仅dsh一审；要求的二审/独立终审未完成且原终审人为作者，恢复review避免当最终接受"})
    card["status"] = "review"
if card.get("final_reviewer") == card["owner"]:
    card.setdefault("final_review_history", []).append({"date": "2026-10-26", "from": card["final_reviewer"],
        "to": None, "reason": "作者不能终审自己的卡，需另分配独立终审人"})
    card["final_reviewer"] = None
card["acceptance_stage"] = "technical_rechecked_pending_independent_final_assignment"
if not by_id["ALN-18-L"].get("delivered_commit"):
    by_id["ALN-18-L"]["delivered_commit"] = "8ca1ba1f03a0b286cda92107b6e502511498d8f0"
for commit, title in ((first_fix, "fix(scheduler): 扫描完整待办账本并拒绝无限调度配置"),
                      (fix, "fix(scheduler): 宿主调度消费主动准入并保留时间类型校验")):
    entry = {"id": "review-fix-"+commit[:12], "title": title, "workpackage": "ALN-13", "source_commit": commit,
        "files": git("diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines(),
        "status": "source_registered_pending_validation", "candidate_included": False,
        "independent_review": False, "report": report, "scope": "13-S调度边界修复；固定相关522过，待独立补丁复核"}
    if not any(e["id"] == entry["id"] for e in board["post_candidate_changes"]):
        board["post_candidate_changes"].append(entry)
batch = {"id": "REVIEW-2026-10-26-ASSIGNED-THIRTEEN", "date": "2026-10-26", "reviewer": "codex-runtime",
    "cards": list(coverage["cards"]), "fixed_cards": ["ALN-13-S"], "fix_commit": fix,
    "fix_commits": [first_fix, fix], "report": report, "evidence": evidence_path,
    "status": "technical_rechecks_complete_fixes_pending_independent_acceptance", "product_acceptance_added": False,
    "published_by_review": False, "scope": "13项逐卡技术结论；后端522/1整机饱和未跑，客户端459同一子树/tsc0；30-A作者终审冲突纠正，恢复review"}
if not any(e["id"] == batch["id"] for e in board["code_review_batches"]):
    board["code_review_batches"].append(batch)
board_file.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")
package_file = root / ".planning/2026-10-26-review-handoff/review-package.json"
package = json.loads(package_file.read_text(encoding="utf-8"))
for identity, result in conclusions.items():
    entry = next((c for c in package["cards"] if c["id"] == identity), None)
    if entry is None:
        entry = {"id": identity, "commit": by_id[identity]["delivered_commit"], "reviewer": by_id[identity].get("reviewer"),
                 "final_reviewer": by_id[identity].get("final_reviewer"), "scope": "受限跨文件AST重构与诊断，非语义LSP"}
        package["cards"].append(entry)
    entry["technical_review"] = result
    entry["gaps"] = gaps[identity]
    entry["final_reviewer"] = by_id[identity].get("final_reviewer")
package["latest_technical_review"] = {"date": "2026-10-26", "reviewer": "codex-runtime", "cards": 13,
    "source_commit": fix, "report": report, "evidence": evidence_path, "independent_acceptance_added": False,
    "author_final_review_conflicts": ["ALN-30-A: claude-main=owner，已清空需重分配"]}
package_file.write_text(json.dumps(package,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")
record = {"date": "2026-10-26", "source_commit": fix, "source_tree": records["committed-validation"]["source_tree"],
    "fix_commits": [first_fix,fix], "report": report, "validation_records": records,
    "results": {"baseline_after_fixture_correction": "15 failed, 2 passed", "guard_baseline": "4 failed, 2 passed, 17 deselected",
                "focused": "49 passed", "restore": "11 passed", "initial_committed": "516 passed, 1 deselected",
                "final_committed": "522 passed, 1 deselected", "client": "459 passed; tsc exit 0"},
    "per_card": conclusions, "client_tree_identity": {"tested_commit": records["validation"]["commit"],
        "backend_commit": fix, "same_android_tree": client_tree},
    "owned_validation_scopes_removed": len(records), "old_caches_dependencies_foreign_work_preserved": True,
    "not_run": ["whole-host CPU saturation", "full backend", "native Kotlin compiler", "new APK/device pressure", "real provider/LLM/production"],
    "log_sha256_lf": {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n",b"\n")).hexdigest() for p in folder.glob("*.txt")}}
(folder/"committed-evidence.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("registered",len(conclusions),"technical reviews; cleanup scopes",len(records),"final review independent")
