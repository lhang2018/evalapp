# ALN-25-A｜有来源的主动帮助与机会建议

- 卡片：[25-A-有来源的主动帮助与机会建议](../任务卡/25-A-有来源的主动帮助与机会建议.md)
- 父包：ALN-25；需求引用：UX-18 / UX-19 / UX-20 / F04（分期计划 PRODUCT-2026-10-25，阶段 3）
- 作者：claude-main；状态：review（待独立审阅，审阅人未定，不自批）
- 认领锚点：base_commit `0cf66722d00ab59f1405c6bca7f322c1bf893a9c`（2026-10-25 认领；期间并行会话持续提交，本卡改动以隔离 worktree 干净 HEAD 验证）
- 依赖：ALN-24-C（accepted，记忆范围控制面）、ALN-24-D（已交付，`memory_scope_policy` 表）

## 目标与范围

复用 `OpportunityService`/`GoalGovernor` 与 24-D 的记忆范围策略，落地 F04
主动整理三步骤的**只读分析服务**：授权工作范围收敛、近期整理（证据四级
分类）与建议交付（高价值门禁 + 四段式格式 + 不自动执行）。出口证据引用
UX-F04-01/02 验收条件；F04-03/04（资源隔离、关闭主动帮助）归 25-B。

## 实现内容

新增 `evoagent/goals/assist.py`（独占新文件，约 380 行）：
`ProactiveAssistService(db, *, memory_control=None, clock=None)`，两个公开
方法、零写入。

### 1. 授权范围（F04 第一步）

- 显式范围规格 `{"goal_ids": [...], "task_ids": [...], "memory_scopes": [...]}`：
  白名单制，**范围外来源不读取**——不因具备读取能力就默认分析全部数据
  （§6.1）。授权目标的任务族按 ALN-14 已接受的递归族查询整体入范围
  （复用 `GoalGovernor._tasks`，不重写 SQL）。
- 记忆作用域过 24-D `MemoryControlService.scope_allowed`（禁止优先）；
  **无策略服务时对记忆 fail-closed** 并记录 `no_scope_policy_service`——
  无法确认授权即不读，与 24-D「禁止优先」同宗。
- 未解析标识（not_found）与拒绝作用域如实登记在 `scope` 记录里，
  不静默丢弃；范围、actor、reason 随结果返回，事实可追溯（F04-01）。

### 2. 近期整理 organize()（F04 第二/三步）

- 证据集合按四级分类，逐条带可解析 `ref`（goal/task/memory + id）：
  - `user_stated`：用户明确说过的内容（记忆 source=user）；
  - `recorded`：有记录支持的任务/目标状态、verified 非用户记忆；
  - `inferred`：治理裁决等系统推断（GoalGovernor.report 的 reason）；
  - `unconfirmed`：冲突 / 过期 / 待复核记忆、过时的非终态任务。
- 六段输出：completed / in_progress / blocked / unresolved_issues /
  confirmed_constraints / time_nodes / pending_confirmation。
- **不把过时状态当作当前事实**：非终态任务超过 `STALE_TASK_SECONDS`
  （3600s）未更新即降级 unconfirmed（stale_state）；记忆冲突经
  fact_key 真实冲突路径（ConflictLedger）产生 has_conflict 后标记待确认；
  过期（valid_until）与待复核记忆同样待确认。被取代记忆经 supersede 置
  revoked，由存储层 listing 排除（ALN-05 已接受切片），不进入输出。

### 3. 建议交付 suggest()（F04 第四/五步）

- 候选来自 `OpportunityService.ranked()`，仅授权范围内的
  `source_ref`（`goal:<id>` / `task:<id>`）进入筛选；范围外候选只计数
  （`excluded_out_of_scope`），不展开细节。
- **高价值门禁**（UX-F04-02）：`ranking_state == "ranked"`（已评估、
  来源已知、置信度 > 0、成本已知）且效用分 > `min_score`（默认 0，
  可配置）；未评估（needs_evidence）、过期、低于门槛分别记入
  `suppressed` 及原因。**无高价值时不输出建议**，返回
  `withheld="no_high_value_opportunity"`——不强行生成无关建议。
- 四段式格式（UX-20 + §6.5）：`what`（发现什么）→ `why`（为何值得关注：
  收益/置信度/成本/风险）→ `how`（怎么做：显式登记为机会目标后走既有
  授权链路）→ `requires`（权限/估计成本/风险/预算检查）。权限段从来源
  目标的授权 Grants 只读解析（tools/permissions/max_risk）；无有效
  Grants 时 `resolved=False` 并声明「执行前须显式授权」，不谎称已授权。
- **建议不等于授权**：每条建议携带
  `authorization={"executed": False, "auto_executed": False, note=...}`；
  服务本身不 submit_task、不 register、不产生任何外部副作用——
  `test_suggest_never_auto_executes` 以任务数/目标数/opportunity
  登记态/副作用账本四重断言锁定。

## 验收证据

出口证据与卡验收三条一一对应：

| 卡验收 | 实现锚点 | 测试 |
|---|---|---|
| 有来源/事实推断未知区分（F04-01） | 白名单范围 + 范围记录 + 四级分类 + 逐条 ref | test_organize_reads_only_authorized_sources、test_unknown_scope_ids_recorded_not_silently_dropped、test_denied_memory_scope_contributes_no_facts、test_memory_without_policy_service_fails_closed、test_every_evidence_ref_resolves、test_evidence_four_levels_classified |
| 无高价值机会可不建议（F04-02） | ranked + min_score 门禁 + withheld | test_suggest_without_high_value_opportunity_outputs_none、test_low_value_candidate_below_threshold_suppressed、test_min_score_gate_is_configurable、test_expired_candidate_suppressed、test_out_of_scope_candidate_excluded |
| 建议含收益、权限和成本，不能自动外部执行 | 四段式 + requires + authorization 字段 | test_high_value_suggestion_four_part_format、test_suggest_never_auto_executes、test_task_sourced_gain_candidate_flows_through_gate（UX-18 出口）、test_suggestion_for_unauthorized_source_states_explicit_authorization_needed |

另覆盖 UX-19 整理语义（test_stale_task_state_not_treated_as_current_fact、
test_conflicting_memory_marked_pending_confirmation、
test_superseded_memory_does_not_surface_as_current_fact、
test_expired_memory_marked_pending_confirmation、
test_candidate_memory_is_inferred_not_confirmed、
test_sections_cover_expected_groups）、输入纪律（actor/reason 必填、
scope 校验参数化 7 例）与失败隔离（§6.4「主动分析失败不会影响核心任务」：
test_failed_analysis_leaves_core_tasks_untouched）。

- 新增测试 **31 项全过**：`tests/test_proactive_assist.py`
  （命令：`python -m pytest tests/test_proactive_assist.py -q`）。
- 相关既有套件复跑 **114 passed**（goal_opportunities / goal_governance /
  user_memory_control / goal_scheduler），无回归。
- **隔离全量**（干净 HEAD + 本卡改动，worktree）：
  **2023 passed / 1 skipped / 0 failed**（536.28s），日志
  `.planning/2026-10-25-aln25a/iso-full-server.log`。
- **变异测试 12/12 捕获、0 存活**（双基线先证全绿）：M1 无策略服务
  fail-open、M2 deny 失效、M3 user_stated 降级、M4 过时当当前事实、
  M5 冲突不标记、M6 过期不标记、M7 高价值门禁移除、M8
  needs_evidence 混入、M9 范围外不排除、M10 授权字段篡改、M11
  权限谎称已解析、M12 withheld 原因缺失；日志
  `.planning/2026-10-25-aln25a/mutation-results.log`，脚本
  `mutation_test.py` 可复跑。
- ruff 对本文件 16 条风格提示——仓库不强制执行（既有
  opportunities.py/user_control.py 同跑 20 条），未追逐，保持与代码库
  一致的既有风格。

## 设计决策与边界

1. **只读服务，不执行**：organize/suggest 不调用 controller、不提交任务、
   不登记目标。F04 规则 5/6（建议≠授权、外部操作须过权限/风险/预算检查）
   由「四段式 requires + authorization 字段 + 无副作用」三层落实；
   登记与执行留给既有显式链路（OpportunityService.register 需操作者）。
2. **记忆 fail-closed**：无 24-D 策略服务时拒绝读取记忆并显式记录，
   而非默认放行——与「禁止优先」和 11-P 的 no_registry 显式记录同族。
3. **被取代记忆不在本服务标记**：supersede→revoked 由存储层 listing
   排除（ALN-05 已接受切片），本服务不重复实现；测试以端到端断言锁定
   「不当作当前事实」。
4. **一目标族一未完成任务**：夹具设计时发现 GoalGovernor 对并发未完成
   任务的 GoalConflict 执法正确，测试按「一任务一目标」分布，未绕过。
5. **UX-18 边界**：「任务执行中实时发现缺口」属运行时主链（07/08 域），
   本卡覆盖其可复用出口——终态任务衍生的增益候选
   （Review failure/Improve outcome）经同一门禁与四段式交付，
   test_task_sourced_gain_candidate_flows_through_gate 锁定。

## 保留缺口（未验证不记通过）

1. **app.py 装配**：`ProactiveAssistService` 未接入 App（app.py 不在本卡
   写入边界；一行接线：构造时注入 MemoryControlService）。
2. **HTTP 端点与持久化**：整理/建议结果目前仅内存返回，未落库、未开
   API（memory_context_api 的同批端点模式可参照）；调用方需自行持久化
   范围与交付记录以满足审计追溯。
3. **App.tsx 接线**：客户端无主动帮助入口（UX-21 主动程度开关归 25-B，
   其接线预计同批）。
4. **UX-F04-03/04**（主任务资源门槛、关闭主动帮助后停启）属 25-B 范围，
   本卡未触及。
5. 真机/生产环境未涉及（本卡为纯本地测试环境卡）。

## 兼容/迁移影响

- 纯新增文件，无共享文件改动；对既有 goals/memory 切片零行为影响
  （隔离全量 2023 全绿佐证）。
- 下游 25-B（资源隔离/主动程度）可直接依赖本服务的 scope 记录与
  suppressed/withheld 结构扩展门禁语义。
