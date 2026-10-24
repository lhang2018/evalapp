# ALN-07-B 运行时动作绑定（effect ↔ step）

2026-10-24最新验收：**按用户明确授权，由codex-runtime统一复验通过既定切片（accepted）**，自己的修复不伪称第三方；范围、修复、真机证据归属和父包后续见[统一验收](../验收/2026-10-24-全部待审核统一验收.md)。下文保留原交付/任务时点。

最新独立审核：**六组问题已修复，cc0aa3a待其他审阅者复核**。19探针原源码15失败/4通过，相关241通过；固定源码1653/1、客户端207/typecheck实际复跑。事务、可信来源、历史缓存、租约和App装配见[独立审核](../验收/2026-10-24-ALN-07-B独立审核.md)。下文是原交付/任务要求，当前行为以修复记录为准。

负责人 claude-main。ALN-07 整包第五项。

## 缺口（认领前核实）

`grep -rn step_bindings evoagent/` 只有 `planning/reconcile.py` 自己命中——
**对账能接收这份绑定，但全库没有任何东西产生它**；`controller.py` /
`engine.py` 里连 `effect_id` 都不出现。07-P 运行时补审的保留边界也明确写着
「同工具多步骤/参数尚无运行时 step_bindings」。

后果是具体的：同一工具被多个步骤声明时，07-E 的对账**永远只能判 `ambiguous`**，
因为它拿不到「这次执行属于哪一步」的证据。

## 交付内容

### 1. `evoagent/core/schema/action_bindings.py`（新表域模块，25 行）

`core/schema/` 按域**自动发现**（`pkgutil`），因此加表是零处注册改动、
不存在「两人同时改注册行」的冲突。

```sql
CREATE TABLE IF NOT EXISTS action_step_bindings (
  effect_id TEXT PRIMARY KEY,   -- 一副作用只归属一步（DB 级硬约束）
  task_id   TEXT NOT NULL,
  step_id   TEXT NOT NULL,
  tool_id   TEXT,
  bound_at  REAL NOT NULL
)
```

`effect_id` 作主键使「一个副作用只能归属一个步骤」成为**数据库层面的强约束**，
而不是仅靠应用层 if 判断。

### 2. `evoagent/planning/binding.py`（`ActionBindings`，144 行）

| 方法 | 作用 |
|---|---|
| `bind(effect_id, step_id, *, task_id, tool_id)` | 记录绑定；幂等；冲突拒绝 |
| `get(effect_id)` | 单条只读查询 |
| `for_task(task_id)` | 导出 `{effect_id: step_id}`，**直接作为 07-E 的 `step_bindings` 入参** |
| `for_step(task_id, step_id)` | 某步骤绑定的 effect 列表 |
| `count(task_id)` | 计数 |

三条一致性规则：
- 同一 `(effect_id, step_id)` 重复绑定 = **无操作**，且不刷新 `bound_at`
  （首次归属时间才是审计上有意义的时刻）；
- 同一 `effect_id` 绑到不同 `step_id` 或不同 `task_id` → `BindingConflict`，
  **原绑定不被覆盖**（归属一旦确立不允许被后写改写）；
- 同一 `step_id` 可以有多个 `effect_id`（一步多次调用工具是正常的）。

### 3. `evoagent/react/engine.py`（可选注入 + 约 15 行接线）

构造器新增**可选**参数 `bindings: Any = None`；工具执行、拿到结果后，
若「有绑定账本 + 有活动步骤 + 结果带 `effect_id`」则记录绑定。

**失败语义**（重要设计取舍）：绑定写入失败**不改变已发生的执行结果**——
副作用已经发生，把真实成功改判成失败更糟；但也不静默吞掉，错误写入
`result.evidence["step_binding_error"]`。无任务范围时不绑，
`evidence["step_binding_skipped"]="no_task_scope"`（**不猜**）。

## 验收留证（新增 12 项，逐条覆盖任务卡八条）

| 验收 | 测试 | 结果 |
|---|---|---|
| 1 幂等 | `test_same_pair_is_noop` | PASS |
| 2 冲突拒绝且不覆盖 | `test_conflicting_step_rejected`、`test_conflicting_task_rejected`、`test_one_step_may_own_many_effects` | PASS |
| 3 输入校验不落半条 | `test_empty_identifiers_rejected`（4 种非法值 × 3 个字段） | PASS |
| 4 导出可直接用 | `test_for_task_shape`（含其他任务不混入） | PASS |
| 5 与 07-E 集成 | `test_ambiguity_resolved_by_binding`、`test_binding_cannot_forge_attribution` | PASS |
| 6 不越界 | `test_ledger_and_plan_untouched` | PASS |
| 7 运行时接线 | `test_binding_recorded_for_active_step`、`test_no_binding_without_active_step` | PASS |
| 8 接线零差异 | `test_without_bindings_same_outcome`（同场景开/关对照） | PASS |

几个关键实证：

- **验收 5 的正向**：同一工具两个 done 步骤 + 两条 confirmed 记录，
  无绑定时对账判 `ambiguous`（`ok=False`）；注入 `for_task()` 导出后
  `ok=True`，且归因为 `(e1,s2),(e2,s1)`——**交叉绑定按实际归属生效，
  不是按序对调**。
- **验收 5 的反向**：把 `e1`（`file_write`）绑到声明 `http_post` 的 `s2` 上，
  07-E 直接抛 `ReconciliationError`（"步骤绑定工具不一致"）。
  这证明**绑定不能用来伪造归因**——真正的责任方是 07-E 的校验，
  本卡只是不提供「随便写就能洗白」的通道。
- **验收 8**：同一场景分别在「不传 bindings」与「传 bindings」下跑，
  比对 `finished` / `answer` / `completed_work` / 各步骤状态，完全一致。

## 回归

- 新增 12 项 + engine/执行边界/运行时补审/07-E 对账/计划修订/进程恢复/计量：
  **136 passed**。
- 改动范围：`engine.py` 仅 1 处删除、28 行增（含注释），其余三个文件全新。
- 干净复本固定 `827d46d` 全量：**1614 passed / 1 skipped（195.16s）**。
  比接线前的 1602/1 恰好多 12，正是本卡新增的测试数——无回退、无替换。

## 写入边界（明确保留）

- **不是授权**：绑定存在不代表动作被允许；本卡不参与任何放行/拒绝判定，
  不改变工具可见性。真正决定「能不能执行」的仍是 01-A 的统一网关。
- **不改账本**：不 transition / reconcile / record 副作用，不释放 pending。
- **不改计划**：不写 `completed_work`、不改步骤状态。
- **不猜**：只记录调用方给出的 `(effect_id, step_id)`；不解析幂等键、
  不按工具名反推归属。无活动步骤时不产生绑定。
- **不校验参数/版本**：绑定只说明「哪一步触发」，不证明「参数是否合规」。
- 精确步骤授权（执行前就拒绝不该执行的步骤，而不是事后归因）仍属后续卡；
  运行时如何消费对账结论（告警/阻断/重试）同样另验。

## 精确提交

`827d46d`：`evoagent/core/schema/action_bindings.py`（25）、
`evoagent/planning/binding.py`（144）、`evoagent/react/engine.py`（+28/-1）、
`tests/test_action_binding.py`（300）。

不自批，交 codex-runtime 独立复核。
