"""Register atomic asset writes and preserve the already-committed peer check."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
source = "518aa26125e582cc75d9f95c97d7d14030b7e161"
report = "docs/协作开发/验收/2026-10-27-资产仓储共享事务复核.md"
evidence = ".planning/2026-10-27-asset-transactions-review/committed-evidence.json"
peer_report = "docs/协作开发/验收/2026-10-27-dsh二次确认-资产事务.md"
records = {name: json.loads((folder / (name + ".json")).read_text(encoding="utf-8"))
           for name in ("negative-validation", "focused-validation", "extra-validation", "final-validation", "committed-validation")}
for record in records.values():
    owned = Path(record["owned_temp_directory"])
    assert record["temporary_data_removed"] and not owned.exists()
    assert owned.resolve().parent == (root / "outputs").resolve()
assert records["committed-validation"]["snapshot_commit"] == source and records["committed-validation"]["exit_code"] == 0
assert "243 passed" in (folder / "committed-tests.txt").read_text(encoding="utf-8")
assert "no tests ran" in (folder / "final-tests.txt").read_text(encoding="utf-8")
peer = {"commit": "803f5bc", "reviewer": "dsh-lead", "report": peer_report,
    "evidence": ".planning/2026-10-27-dsh-asset-tx-confirm/evidence.json", "result": "118 passed；4独立探针通过",
    "scope": "518aa26事务所有权修复点二次确认；非27-B/06-A完整最终接受"}
assert (root / peer_report).is_file()
entry = {"date": "2026-10-27", "source_commit": source, "fix_commits": [source], "report": report,
    "validation_records": records, "results": {"negative": "28 failed", "focused": "106 passed",
    "extra": "3 passed, 28 deselected", "fixed_related": "243 passed"},
    "new_probes": 31, "independent_patch_confirmation": peer,
    "owned_validation_scopes_removed": 5, "independent_acceptance_added": False,
    "initial_fixed_attempt": "Nonexistent test_aln06_acceptance.py: no tests ran; original final-* retained, committed-* is final evidence",
    "not_run": ["full backend", "client/TypeScript", "device/APK", "real process kill", "production concurrency/storage"],
    "foreign_work_preserved": True,
    "log_sha256_lf": {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in folder.glob("*.txt")}}
(folder / "committed-evidence.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(root / report).write_text(f"""# 资产仓储共享事务复核

2026-10-27，codex-runtime。接续26f2ead，从27-B/候选宿主消费者下沉核对AssetRepository；修复 **{source}**。原06-A接受历史保留，因新共享缺陷重开review；27-B维持review，原owner/reviewer不变。期间803f5bc已有dsh-lead非作者二次确认，记录其来源与范围，不替代整卡终审。

## 确认问题

| 编号 | 级别 | 触发与影响 |
|---|---|---|
| AT-01 | P1 | 构造/登记/幂等登记/迁移/晋升/依赖检查/GC无条件commit，连同宿主无关待定数据提前落库。上层rollback失效；真实compose也会把本应随宿主回滚的组合草稿提交 |
| AT-02 | P1 | 登记/生命周期变更/GC先改资产再写审计，审计SQL失败后不回滚；后续无关commit会把半写资产提交。晋升内部依赖审计还提前commit，破坏整笔原子性 |
| AT-03 | P2 | schema初始化分步自动落DDL，第三步失败留下部分表/索引，且缺宿主事务所有权保护 |

## 修复行为

统一_write事务边界覆盖全部写入口及建表：独立调用BEGIN IMMEDIATE并自持提交，宿主已有事务时使用唯一SAVEPOINT；失败/中断/提交错误只回滚自身范围，不提交或抹掉宿主待定工作。内部依赖审计不再提前commit，资产与成功审计同笔提交/回滚。

**失败依赖检查仍留审计**：缺依赖/环/pin不匹配这类语义失败，没有资产状态变更，按原契约记录失败审计再抛DependencyError。独立调用提交该审计，位于宿主事务中则审计也由宿主最终决定；不会用“遇异常全回滚”抹去既有失败证据。SQL/中断失败依然回滚本次操作。

内容摘要、不可变身份、生命周期/来源证据门槛、GC引用保护与运行时精确绑定条件未放宽，不新增执行权限。

## 验证

- 原28条新SQLite/跨连接探针修前 **28失败**，修后与资产身份/组合/消费回归 **106通过**。
- 补3条护栏：宿主事务中的建表失败，以及事件写后KeyboardInterrupt在独立/宿主事务两种情况下的清理；**3通过/28未选**。这是合成中断，不是OS进程kill。
- 新31探针均进入固定518aa26 Git归档 **14相关模块243通过**：资产/GC/组合、发布资产与通道、App装配、编码消费、候选宿主/队列与v3基准/API。证明独立调用正常提交、父事务提交/回滚均正确、审计失败不留半写、宿主无关数据不受损。
- 首次固定回归误列不存在test_aln06_acceptance.py，**没有运行测试**。原final-tests/final-validation保留；纠正后的committed-tests/committed-validation才是最终证据。

[固定证据](../../../{evidence})含完整参数、归档/源码树摘要及日志。五处本轮新建验证临时范围全部回收。不是后端全量；客户端/TS、真机/APK、真实kill与生产多进程/存储未验。发布通道测试来自固定源码，不代表验收其他会话未提交的通道修改。

## 审阅与接续

803f5bc的[dsh二次确认](2026-10-27-dsh二次确认-资产事务.md)已经记录118通过和4项真实repository/连接探针通过，范围是518aa26修复点，不是最终卡接受；该历史原样保留。

06-A保留早期accepted证据，当前review等待新共享补丁的最终复核；原owner=claude-main/reviewer=codex-runtime不重写，修复人codex不自批。27-B原owner=zcode-main/reviewer=zcode-next及生产展开/收益缺口保留；06-R既定接受不因相关消费者回归扩展为新生产接受。父包/发布基线不关闭或变更。

旧缓存/依赖、Android更新/发布通道、UI/matrix、其他人的指派与审核在途内容保留。共享元数据只提交06-A/27-B自身增量。
""", encoding="utf-8")


def append(path, heading, body):
    target = root / path
    current = target.read_text(encoding="utf-8")
    if heading not in current:
        target.write_text(current.rstrip() + "\n\n" + heading + "\n\n" + body + "\n", encoding="utf-8")


append("docs/协作开发/交付/ALN-06-A-不可变资产基础.md", "## 2026-10-27 共享事务接续修复",
    "518aa26统一建表/登记/迁移/依赖审计/GC的事务所有权，禁止提前提交宿主事务和审计失败半写；31新探针、固定14模块243通过。原接受历史保留，当前review待最终复核；已有dsh-lead二次确认118/4探针，不自批。见[复核报告](../验收/2026-10-27-资产仓储共享事务复核.md)。")
append("docs/协作开发/交付/ALN-27-B-可组合能力与动态工作流资产.md", "## 2026-10-27 仓储事务消费复核",
    "518aa26修复真实compose在宿主事务中的提前提交，组合草稿与登记审计现在随宿主回滚；固定相关243通过。已有803f5bc二次确认118/4探针，原分配终审及生产展开/收益缺口仍待；见[复核报告](../验收/2026-10-27-资产仓储共享事务复核.md)。")
append("docs/协作开发/待接需求_2026-10-26.md", "## 二十五、资产仓储共享事务接续复核（2026-10-27）",
    "518aa26修复资产仓储建表/登记/幂等/晋升/依赖审计/GC误提交宿主事务及故障半写。31新探针，固定14相关模块243通过；5处新验证范围回收。见[报告](验收/2026-10-27-资产仓储共享事务复核.md)。\n\n06-A原accepted历史保留、当前重开review；已有803f5bc dsh非作者二次确认118/4探针，仍需最终卡复核。27-B保持review及原owner/reviewer；生产组合展开/收益、真实多进程存储与设备仍待。不扩大06-R既定接受、不改变父包或发布基线。")
board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
before = copy.deepcopy(board)
delta = {"cards": {}, "post_candidate_changes": [], "code_review_batches": []}
for card in board["dispatches"]:
    cid = card["id"]
    if cid not in ("ALN-06-A", "ALN-27-B"):
        continue
    previous_status = card["status"]
    fields = {"status": "review", "technical_review": {"date": "2026-10-27", "reviewer": "codex-runtime",
        "source_commit": source, "source_tree": records["committed-validation"]["source_tree"], "report": report,
        "evidence": evidence, "status": "patch_rechecked_pending_final_acceptance", "result": "31新探针；固定14模块243通过",
        "independent_patch_confirmation": peer, "independent_acceptance_added": False},
        "latest_code_review": {"id": "REVIEW-2026-10-27-ASSET-TX", "date": "2026-10-27", "reviewer": "codex-runtime",
            "reviewed_commit": source, "report": report, "status": "patch_rechecked_pending_final_acceptance"},
        "next_step": "518aa26仓储事务修复固定243通过，803f5bc已有dsh二次确认118/4；最终卡接受及生产/组合展开缺口仍待；" + report,
        "blocker": "共享仓储新补丁已有非作者二次确认、最终卡接受仍待；生产多进程/设备及组合展开缺口不因此关闭"}
    if cid == "ALN-06-A":
        fields["review_verdict"] = "reopened_patch_rechecked_pending_final_acceptance"
    item = {"date": "2026-10-27", "reviewer": "codex-runtime", "source_commit": source, "report": report, "log": evidence,
        "scope": "仓储事务所有权/保存点、资产与审计原子、真实compose父事务回滚", "result": "固定243通过；已有dsh二次确认，非整卡最终接受"}
    history = {"date": "2026-10-27", "previous_status": previous_status,
        "previous_technical_review": copy.deepcopy(card.get("technical_review")),
        "reason": "新共享仓储缺陷修复，早期接受/审阅保留；518aa26已有803f5bc二次确认，待最终卡复核", "report": report}
    card.update(copy.deepcopy(fields))
    card["evidence"].append(item)
    card.setdefault("review_history", []).append(history)
    delta["cards"][cid] = {"fields": fields, "evidence": item, "history": history}
change = {"id": "review-fix-" + source[:12], "title": "fix(assets): 隔离宿主事务并原子保存资产与审计", "workpackage": "ALN-06",
    "source_commit": source, "files": subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", source], cwd=root, text=True, encoding="utf-8").splitlines(),
    "status": "source_registered_pending_validation", "candidate_included": False, "independent_review": False,
    "independent_patch_confirmation": peer, "report": report, "scope": "243相关；已有修复点二次确认，非整卡/基线接受"}
batch = {"id": "REVIEW-2026-10-27-ASSET-TX", "date": "2026-10-27", "reviewer": "codex-runtime",
    "cards": ["ALN-06-A", "ALN-27-B"], "fixed_cards": ["ALN-06-A"], "fix_commit": source, "report": report, "evidence": evidence,
    "status": "patch_rechecked_pending_final_acceptance", "independent_patch_confirmation": peer,
    "product_acceptance_added": False, "published_by_review": False, "scope": "资产仓储共享事务与组合消费；06-A保留旧接受后重开，27-B维持review"}
for key, item in (("post_candidate_changes", change), ("code_review_batches", batch)):
    assert not any(c["id"] == item["id"] for c in board[key])
    board[key].append(item)
    delta[key].append(item)
assert {c["id"] for c, old in zip(board["dispatches"], before["dispatches"]) if c != old} == {"ALN-06-A", "ALN-27-B"}
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
(folder / "metadata-delta.json").write_text(json.dumps(delta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("registered asset fix and two-card review;243 passed;peer check preserved;five validation scopes removed")
