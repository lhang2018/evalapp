# ALN-07-C 交付：原子绑定、精确步骤授权与对账消费

任务：ALN-07-C 原子绑定、精确步骤授权与对账消费
负责人：zcode-next
审阅人：zcode-main（按当前台账；一审 dsh-lead，新补丁须独立复核）
基线 commit：`d8ee355`（认领锚点）；分支 feature/user-direction，交付 commit 见 git log "feat(ALN-07-C)"
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：07-B 的 effect→step 绑定在「副作用已落账、绑定未写」
  窗口崩溃后永久缺失，07-E 对账只能判 ambiguous；07-E 对账器交付后
  无任何生产消费者——接管恢复（recover_task）不做对账直接续跑。
- 修改前：绑定窗口内崩溃 → 半绑定（effect 无归属）不可恢复；恢复
  路径与对账零集成，副作用分歧任务可被自动续跑。
- 修改后：
  1. **预绑定意图**：副作用工具调用**前**登记 intent（新表
     `action_binding_intents`），执行成功后回填 effect_id 并固化权威
     绑定（相邻写）。任何时刻崩溃，意图流都能证明「该步骤当时正要
     产生该工具副作用」。
  2. **确定性补绑 heal_pending**：崩溃恢复时按（task/tool 匹配 +
     effect 未绑定 + 步骤无其它绑定）规则补绑；唯一候选补绑、零候选
     标 stale（迟到落账可再补）、多候选 ambiguous 不自动绑。
  3. **恢复对账门禁**：recover_task 的接管恢复（recovery=True）在
     续跑前执行「补绑 → 计划声明×账本×绑定三方对账」；divergence_free
     放行，存在 undeclared/unexecuted/uncertain/ambiguous 分歧时
     保守转 WAITING_USER（禁止自动重放，R-12），发
     `reconciliation_gate` 事件携带完整分歧明细。

## 与既有层的分工（重要）

- leases 恢复检查（04-R）已拦「未决副作用」（planned/submitted/
  unknown → RecoveryBlocked → SUSPENDED）；本卡门禁处理其放行后的
  **已决分歧**（confirmed 未声明 / 声明完成但账本无记录 / 多声明歧义）。
  两层互补，e2e 测试分别覆盖。
- 07-E 语义澄清（测试中固化）：confirmed 副作用与声明**同工具**即按
  工具归因 matched——绑定只在多声明同工具时消歧；「undeclared 分歧」
  指账本 confirmed 工具不在任何声明中。

## 范围

- 独占新增：`tests/test_binding_atomicity.py`（13 项）。
- 修改：
  - `evoagent/core/schema/action_bindings.py`：新表
    `action_binding_intents`（幂等 CREATE，域内新增，无迁移）；
  - `evoagent/planning/binding.py`：record_intent / resolve_intent /
    intent / pending_intents / heal_pending（绑定语义与 07-B 的
    幂等/冲突约束一致）；
  - `evoagent/react/engine.py`：副作用工具执行前登记意图（纯观察
    工具不登记；意图失败不阻止执行——它是证据流不是许可闸门），
    成功路径 bind → resolve_intent 回填固化；
  - `evoagent/core/controller.py`：`_reconciliation_gate` +
    recovery 接线（放行返回 None / 分歧返回 WAITING_USER 任务 /
    计划不可读返回 FAILED / 无计划任务放行并标注
    `reconciliation_skipped`）。
- 未动：授权发放链（04 语义：恢复复用 grant、丢失禁止重发——
  e2e 断言对账拦截后零新授权）；app.py 装配（bindings 既有注入）；
  他在途文件（server.py / budget.py / test_goal_budget_migration.py）。

## 验证证据

- 专项：`python -m pytest tests/test_binding_atomicity.py -q` →
  **13 passed**（意图登记/回填/幂等/改写拒绝；崩溃唯一补绑/零候选
  stale/多候选不自动绑；干净放行/confirmed-undeclared 分歧
  WAITING_USER 且零工具重放/崩溃意图补绑后干净续跑/无计划任务标注
  放行/recover_task e2e 两路径/对账拦截后零新授权）。
- 相关回归：test_action_binding + peer_action_binding_review +
  plan_side_effect_reconcile + peer_plan_reconcile_review +
  run_ownership + run_continuation + async_approvals +
  execution_boundaries → **156 passed**。
- 全量：`python -m pytest tests -q --ignore=tests/test_goal_budget_migration.py`
  → 结果见下方补记（该文件为并行会话 15-P 在途半成品，
  IndentationError 收集失败，非本卡范围，排除理由如实登记）。
- 边界：全部本地自动化（内存/临时 SQLite，崩溃以「意图已登记、
  resolve 未执行」模拟）；多进程真实 kill 注入与生产租约演练按
  整包/17-P 范围另验。

## 回滚与未完成事项

- 回滚：revert 单提交；intents 表独立于既有 bindings 表，无迁移逆转。
- 未完成（按任务卡原范围保留）：
  1. 用户对 WAITING_USER（reconciliation）的裁决入口（answer 消费
     phase=reconciliation 的 waiting_request，属 04-UI/23-E 范围）；
  2. heal_pending 多候选 ambiguous 的人工裁决 API；
  3. 同一恢复内 gate 与 leases 两层门禁的统一事件视图。
- 影响主题：R-12（副作用账本 unknown 不重放→扩展至已决分歧不自动
  续跑）、R-11（恢复前重核外部状态）、C-06（轨迹绑定证据完整）、
  E-03（对账只判定不修复）。

## 审阅结论

待审。建议重点：意图登记「尽力而为」与权威绑定失败语义的边界；
heal 规则中「步骤已有绑定则排除候选」的防重复归因论证；gate 在
leases 未决副作用层之后的分工互补性。
整包结论：仍实现中。


## 第五批复核（2026-10-25）

复核源码3331898fcddb78b4b271db152fbd408b7c981889。本批只读复跑竞争意图、故障回滚与既有恢复/租约回归，未发现新阻断项。此前0cf6672修复由本会话编写，本记录不作为独立接受，不改绑定实现。

新修复待独立复查，详见[第五批复核](../验收/2026-10-25-第五批补绑凭据与重构复核.md)。

## 2026-10-27 恢复消费接续修复

7de95f7修复只读tool_hint误当副作用声明、门禁异常活动登记泄漏、非法FAILED转换。新13项探针，固定相关241 passed，最终聚焦86通过；旧13/156/169数字均为历史，不替代新源证据。详见[接续报告](../验收/2026-10-27-恢复对账消费边界复核.md)。原owner与分配reviewer zcode-main、一审dsh-lead保留；新补丁需独立确认，人工裁决/多候选API/真实kill/统一视图仍待。

## 2026-10-27 裁决持久化接续核验

33c22a1补齐SUSPENDED分歧→WAITING_USER合法边，状态与waiting_request/failure_reason一并保存，避免状态事件故障留下空裁决。16新正式探针修前9失败/7通过，聚焦54通过，固定13模块214通过。详见[接续报告](../验收/2026-10-27-恢复裁决持久化接续核验.md)。原分工与review保留，新补丁待独立复核；人工裁决语义/再次对账、多候选API/真实kill仍待。
