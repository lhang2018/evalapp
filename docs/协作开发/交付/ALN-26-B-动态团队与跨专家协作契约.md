# ALN-26-B 动态团队与跨专家协作契约

任务：ALN-26-B / 动态团队与跨专家协作契约
负责人：dsh-lead（本会话认领，先登记 claim 再实现）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`6a8501233f0ec0f7b4fce74d97fd13facd6e6b0d`（认领时 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `37465abf86b6fdc844d107cd3a3c65bdf9d2156a`（tree `6c35a10a95ce1fcd0e50be8d18975b7e44957b3a`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：一个真实任务需要**多个专家协作**（如编程 + 旅行规划），必须明确：谁负责什么能力、输入输出怎么交接、状态由谁唯一写入、能力缺失/归属冲突如何解释、某个角色局部失败后能不能自动重放外部动作。
- 修改前：
  - `ExpertRegistry` 已提供专家/能力目录、路由与影响分析；planning 有意图/规划/匹配/契约；但**没有团队契约层**：没有角色指派与能力覆盖校验、没有「唯一状态写入者」、没有显式跨专家交接（输入引用/上下文范围/预算）、也没有失败隔离与「不越权重放外部动作」的契约表达。
- 修改后（新增 `evoagent/experts/team.py`，**未改** `registry.py`/`planning/*`/`workflows/*`）：
  - `TeamPlanner.form()`：按意图/领域 + 必需能力从**真实能力目录**成组：
    - 能力缺口 → `TeamFormationError` 列出缺失能力；
    - 同一能力多个候选 → 域专家优先、意图匹配优先；仍歧义则**拒绝并列出候选**（冲突可解释）；
    - 每个角色带能力、职责、输出、**唯一状态物**、外部动作声明。
  - `TeamContract.validate()`：拒绝重复状态写入者、校验交接引用与角色存在性；`digest()` 稳定可复核。
  - `advance()`：**只有唯一状态写入者**能推进 `proposed→active→completed`；失败角色不能推进；终态拒绝（单一状态机）。
  - `Handoff` + `TeamPlanner.add_handoff()`：显式「生产角色→消费角色」，输入必须属于生产角色输出、禁止自交接、带上下文范围与预算份额；破坏契约则拒绝并回滚。
  - `fail()` / `recovery()`：局部失败只标记该角色；**`auto_replay=False`**，存在外部动作时**必须人工确认**；`authorize_external_action()` 只有属主角色可执行，失败角色需 `confirmed=True`，其它角色不能代执行（不越权）。

## 范围

- 独占模块：`evoagent/experts/team.py`（新增）。
- 共享文件补丁：**无**（未改 registry/planning/workflows 任何共享文件；21-A 收口在途，避免并行覆盖）。
- 新增/修改契约：团队角色、协作契约、交接、失败恢复四类结构；无 schema 变更、无迁移（契约当前为内存对象，持久化属后续接线）。
- 兼容：纯新增模块，既有专家/规划/工作流行为不变。

## 验证证据

- 正向（真实 builtin 能力目录）：
  - `coding + travel` 两个真实专家成组，能力覆盖 `code_gen`/`itinerary`；
  - 每个状态物唯一写入者；契约 `validate().ok=true`；
  - 显式交接 `role_coding → role_travel`（输入来自生产角色输出、预算 0.5）；
  - 唯一写入者推进 `proposed→active`；`digest` 稳定且随交接变化。
- 拒绝/失败路径：
  - 缺失能力 `no_such_cap` → 拒绝并列出缺失；
  - 能力归属歧义（两个专家同能力同意图）→ 拒绝并列出 `alpha/beta` 候选；
  - 交接输入不属于生产角色输出 / 自交接 / 引用不存在角色 → `TeamContractError`；
  - 重复状态写入者 → `validate()` 报「多个写入者」；
  - 非写入者推进 → 拒绝（「非唯一状态写入者」）；
  - 局部失败：失败角色无法推进契约；`recovery.auto_replay=false`、`requires_confirmation=true`，外部动作逐条列出；失败角色须 `confirmed=True` 才能重放，其它角色不能代执行。
- 命令、结果、日志与对应源码 commit：
  - 提交 `37465abf86b6fdc844d107cd3a3c65bdf9d2156a`；
  - 本卡专项：`python -m pytest tests/test_team_contract.py -q` → **11 passed**；
  - 专家/规划/路由域回归（test_expert*/test_planning*/test_routing* + 本卡）→ **68 passed**；
  - 证据生成：`.planning/2026-10-25-aln26b/team-contract-evidence.json`，**13 项检查全过**（`all_checks_pass=true`），含真实 registry 成组、交接、唯一写入者推进、失败隔离与不越权；
  - 全量后端：`python -m pytest tests/ -q` → **2123 passed / 0 failed / 1 skipped（374.91s）**，日志 `.planning/2026-10-25-aln26b/backend-tests.txt`（本轮全绿）。
- Mock / 夹具 / 真实边界：
  - 能力目录与专家为**真实 builtin registry**（非夹具）；冲突/歧义场景使用显式构造的 registry（测试内标注）；
  - **契约层尚未接入运行时主链**：`planning/planner.py`、`workflows/engine.py` 未改，因此真实跨专家任务**执行**还没有消费契约；本卡交付的是契约与校验层；
  - 外部动作是契约声明，未接实际工具权限系统；契约未持久化（内存对象）；
  - 未做多角色并发执行/真实预算扣减验证。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert 37465abf86b6fdc844d107cd3a3c65bdf9d2156a`（删新增模块与测试；无 schema/迁移）。回滚后既有专家/规划行为不变。
- 保留的限制、后续 ALN 和依赖：
  - **运行时接线**（真实任务按契约成组、交接与状态机执行、契约持久化）未完成，属 26-B/21-A 收口范围；
  - 外部动作未与实际工具/审批系统绑定；
  - 预算份额只是契约字段，未接 BudgetLedger；
  - 26-A 的缺口计划与 26-B 的团队契约尚未互相引用（缺口→团队组建链路属后续）。
- 影响的主题/实体/ADR/契约/系统验收条目：新增团队协作契约结构；既有专家/规划契约不变；A-53/A-60 的「团队角色/输入输出/冲突/失败恢复」服务端契约部分交付。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-26：运行时接线与真实跨专家任务执行仍待；本卡交付团队与跨专家协作契约层）


## 第四批审查修正（2026-10-25）

修复提交6ae4b30525e30e6f7278953a0c5e8772e4727dcb。推进/动作先校验完整契约，所有状态写入者确认后才完成团队；严格交接范围/预算/确认类型，冻结角色集合。仍为内存声明契约，不代替生产身份/工具审批、预算扣减或真实并行执行。

新增修复待固定提交独立复核；旧证据保留为历史，详见[第四批审查](../验收/2026-10-25-第四批主动规划与组合资产审查.md)。
