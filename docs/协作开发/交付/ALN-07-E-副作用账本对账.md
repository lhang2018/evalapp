# ALN-07-E 副作用账本对账（计划↔已执行副作用）

负责人 claude-main。ALN-07 整包第四项，接续 07-P / 07-W / 07-T / 07-R。

## 缺口（认领前核实）

全库不存在任何「计划↔副作用账本」的自动对账实现（`reconcile_plan` /
`plan_reconcile` / `expected_effects` 均无命中）。07-T 能回答「哪些步骤已完成」，
但答不了下一个问题：**这些步骤实际有没有按声明产生副作用，以及有没有产生
计划之外的副作用**。ALN-07 整包 next_step 明示此项为遗留待办。

## 交付内容

新增 **`evoagent/planning/reconcile.py`**（393 行，纯新文件）。

### 数据契约

| 类型 | 用途 |
|---|---|
| `DeclaredEffect` | 计划步骤声明的预期副作用（源自 `PlanStep.tool_hint`），携带 `step_state` |
| `EffectRecord` | 账本记录的只读投影（`effect_id/tool_id/status/idempotency_key/...`） |
| `MatchedEffect` | 计划声明与账本记录的成功归因（三者绑定） |
| `UndeclaredEffect` | 未声明的执行 + `reason` 区分「无匹配声明」与「超出声明次数」 |
| `ReconciliationReport` | 对账结论：`summary` 计数、`findings()` 可读偏差清单、`ok` 判定 |

### 五类归类

| 类别 | 含义 |
|---|---|
| `matched` | 计划声明 ∩ 账本已确认（`confirmed`） |
| `undeclared` | 账本有执行、计划未声明该工具；或执行次数超出计划声明次数 |
| `unexecuted` | **`done` 步骤**声明的工具在账本无任何记录（声称完成却无证据） |
| `uncertain` | 账本状态为 `planned`/`submitted`/`unknown`/`failed`——**绝不计入 matched** |
| `pending_declarations` | 非 done 且非 skipped 步骤的声明（时机未到，如实区分而非混为一谈） |

`ok` = 无 undeclared、无 unexecuted、无 uncertain。

### 三条硬约束

1. **结构性纯读**：主入口 `reconcile(plan, records)` 只接受内存里的记录列表，
   **不持有 `SideEffectLedger` 引用**——不导入账本/持久化模块，物理上无法写入。
   便利入口 `reconcile_ledger()` 只调用 `ledger.history()` 这一个只读查询。
2. **fail-closed**：未决记录（含 `failed`）一律归 `uncertain`，**"没确认"不等
   于"没发生"**，不会被当成「安全无事」。
3. **确定性**：所有输出列表按天然键排序；阻断妨碍 matcing 的输入次序
   （已用 12 轮随机打乱验证）。

### 不猜边界

只按 `tool_id` **字面**归因。`file_write` 与 `file.append` 是否等价由调用方判定，
本模块把未匹配项原样暴露，而不是猜测等价后悄悄计入 matched。

## 验收留证（新增 16 项，逐条覆盖任务卡七条）

| 验收 | 覆盖测试 | 结果 |
|---|---|---|
| 1 完整归因 | `test_every_record_is_accounted`、`test_missing_tool_id_rejected` | PASS |
| 2 未执行不冒充成功 | `test_done_step_without_record_is_unexecuted`、`test_pending_step_declaration_is_not_unexecuted`、`test_skipped_step_excluded` | PASS |
| 3 未决不算完成 | `test_unsettled_statuses_never_match`（planned/submitted/unknown 三种）、`test_failed_record_is_not_completion` | PASS |
| 4 越权检出 | `test_undeclared_tool_flagged`、`test_execution_exceeding_declaration_count` | PASS |
| 5 确定性 | `test_shuffled_inputs_same_report`（12 轮随机打乱 plan 与 records） | PASS |
| 6 纯读零写入 | `test_ledger_entry_only_reads_history`（写入即抛错的只读账本）、`test_reconcile_does_not_mutate_inputs`、`test_no_ledger_reference_in_signatures`（AST 检查 import 图） | PASS |
| 7 序列化往返 | `test_roundtrip`、`test_summary_counts_match_lists` | PASS |
| 护栏（成功路径） | `test_clean_reconciliation_ok` | PASS |

刻意保留的成功路径护栏：完全一致的计划与账本必须判 `ok`，防止套件变成
「只验证拒绝、从未验证过任何东西能真正对上」的假绿。

## 回归与无回退

- planning 域组合：**102 passed**（新增 16 + 既有 trace/recovery/planning/
  revision/workflow_matching/export 86）
- **零既有文件改动**：`git status` 显示只有两个未跟踪新文件，`planner.py`、
  `core/side_effects.py`、`core/persistence.py` 全部未被触碰（本卡只读不写）。
- 干净 worktree 固定提交 `c91621e` 全量：**1537 passed / 1 skipped（183.85s）**，
  日志 `.planning/2026-10-24-aln07e/fullsuite-clean.txt`。

> **关于主工作区跑全量的失败（记录备查）**：首次在主工作区跑全量得到
> 「2862 errors during collection」，根因是 `outputs/` 下堆积着历史复核的
> 多个 worktree 副本（`aln05-05a7cf4`、`aln05-5847f3f` 等），同名测试文件
> 触发 `import file mismatch`。**与本次交付无关**——改用干净 worktree 跑
> 固定源码后全量正常通过。教训：本仓库跑全量基线必须用干净 worktree，
> 不能用可能存在历史产物污染的主工作区。

## 写入边界（明确保留）

- **不接运行时**：谁在对账后做什么（重试/告警/阻断）不属于本卡。
- **不改账本**：不 reconcile 副作用、不释放 pending、不补偿任何东西。
- **不做语义等价判断**：见上文「不猜边界」。
- ALN-07 整包的工作流/轨迹/恢复的实际运行时消费仍属其它卡，整包不关闭。

## 精确提交

代码提交 `c91621e`（`evoagent/planning/reconcile.py` 393 行 +
`tests/test_plan_side_effect_reconcile.py` 302 行，纯新增）。

不自批，交 codex-runtime 独立复核。
