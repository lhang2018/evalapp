# EvoAgent 现状 ↔ 正式版 1.0 设计预期 差距分析

**审计日期**：2026-10-21
**基线**：`docs/EvoAgent_全量详细设计正式版_1.0/`（89 主题 + 19 个 A/SV/CTRL 实体 + 14 ADR + 10 类数据契约 + 20 条系统级验收）
**方法**：逐主题源码核验（文件:行号）＋ 专项卷契约密度普查 ＋ 实测测试基线
**定位**：本文件**修正** `IMPLEMENTATION.md` 第 156-329 行「89 主题逐项核对」表的 ✅/🔹/⬜ 判定。修正原因是该表存在系统性**「接口在、逻辑空」**记账偏差——多处 ✅ 只有字段与签名，没有产生该字段的代码路径。
**版本关系**：本文件取代本文件上一版（2026-10-21 上午版，纯聚合 IMPLEMENTATION 表、无独立核验）。上一版独有的「主题外差距」与「优化路线批次」内容已并入本文 §3.14 与 §7。

---

## 0. 结论摘要

> **进度注记（2026-10-22 夜）**：本节数字是 **2026-10-21 审计快照**。
> 此后并行工作流又落地 4 个特性（F-01 错误对象 / F-03 副作用账本 / F-12 审批实体 / F-13 检查点有序化）
> 与 1 个死字段修复（`health_check` 真探针），**基线已变化**：
> 主题表统计仍为 ✅34 / 🔹36 / ⬜19（部分 🔹 的缺口内容已缩小，但主题级状态未变），
> 测试数 475 → 见 `IMPLEMENTATION.md` 实测快照，**缺口闭合校验 0 漏项**。
> 本节保留原始审计结论作为**对照基线**——不修改历史数字，只在此标注演进。

| 维度 | 结论 |
|---|---|
| **名义覆盖率** | IMPLEMENTATION.md 记 ✅59 / 🔹15 / ⬜15（66%） |
| **核验后** | 59 个 ✅ 中 **18 项名不副实**（死字段 / 零调用方 / 恒真分支 / 绕过门禁）；**逐行统计实为 ✅34 / 🔹36 / ⬜19（38%）** |
| **最严重的一条** | **E-07 进化候选生成**：标 ✅「失败/成本异常/质量退化/能力缺口→候选提案」，但四类信号在 `evoagent/` 全目录**零匹配**，全部靠调用方手工传字符串。`register_problem()` 往 deque 里塞完再无人读取。**整个"自主进化"叙事建立在这个空壳上**。（▸ 2026-10-22 演进：四类信号采集器与自动提案链已落地，E-07 转 ✅，见 IMPLEMENTATION.md） |
| **整组空白** | G 组（G-01..G-10）9/10 未实现，系统级验收用例 19 因此永远无法通过 |
| **更根本的发现** | **设计文档本身缺契约**：8 个专项卷 79 个主题中 **79 个（100%）是模板填空**——无字段名、无 JSON schema、无接口路径、无状态枚举、无数值阈值。全部可编码契约只集中在 1 个补充设计文件（19 实体）+ 09 卷契约规范 + ADR/术语表。**"G 组空白"不能靠照文档实现解决，必须先补写契约** |
| **契约层缺口** | 10 类契约中 **Goal、Side-effect Ledger、Capability Asset 三份整份缺失**；统一错误对象 7 个字段 **0 实现**；Event/Tool/Manifest 合计 **17 个字段缺失或形态不符** |
| **测试基线** | 服务端 **443 项**通过；客户端 128 项通过。（首轮基线 358；F-00 +39、F-01 +11、台账校验器 +22、漂移扫描 +13）|
| **计划层漏项**（2026-10-22 新增） | 缺口闭合校验查出 **8 个主题在 ROADMAP 与特性表中双双零覆盖**——不是"还没做"，是"根本没排进计划"。已补为轨道 T6 / 特性 F-51..F-58 |

---

## 1. 实测基线（本次审计跑出来的，非文档转述）

```
服务端：python -m pytest tests/ -q          → 475 passed
        首轮全量 1 failed: test_control.py::TestPauseResume::test_pause_then_resume_completes
        隔离重复 3 次                          → 5 passed / 5 passed / 5 passed  ⇒ 竞态，非确定性缺陷
客户端：node --test test/{offline,device,update}/*.test.ts → 128 passed
```

三点需要注意：

1. **文档数字三处互相矛盾且全部过期**（审计时）：README.md 写「352 Python + 120 客户端」；FEATURES.md 写「352 项」；IMPLEMENTATION.md 写「331 Python + 60 客户端」。实测 358 + 128。**注：本轮已全部更正为 475 + 128**。
2. **工作区处于未提交状态**，含在途新文件 `NotificationModule.kt` / `nativeNotifications.ts` / `notifications.ts` / `notifications.test.ts`（客户端 128 项比文档 120 项多的部分即来自此处）。本次审计未触碰这些文件。
3. **已定位一处真实并发缺陷**（非 flaky 测试问题）：`tests/test_control.py:125` 断言 `cp.current_stage == "paused"` 实际拿到 `'RUNNING'`。根因是 `evoagent/core/controller.py` 写 `paused` 检查点与写 `target.value`（RUNNING）检查点两处无序，`CheckpointManager.latest()` 取到较晚写入的 RUNNING。**暂停语义在检查点层面被后写的运行态检查点覆盖**，这与 Session 4 确立的"暂停后落检查点"设计约束冲突。**✅ 已由 F-13 修复**（并行工作流 `2c0515d`：先落 paused 检查点再持久化 SUSPENDED 状态；修复后连续 3 次全量跑通过）。

---

## 2. 差距总览

### 2.1 名义 vs 核验

> 本表数字已由 `IMPLEMENTATION.md` 落账后的脚本统计复核（2026-10-21 Session 34）。
> 初版此处是**估算值**（✅41/🔹36/⬜12），实际逐行统计为 **✅34/🔹36/⬜19**，已更正。

| 组 | 主题数 | 原台账判定 | **核验后落账** | 调整理由摘要 |
|---|---|---|---|---|
| P 产品与部署 | 8 | ✅6 🔹1 ⬜1 | ✅5 🔹2 ⬜1 | P-08 无 AgentRuntimeAdapter 协议层 |
| R 任务执行内核 | 12 | ✅12 | ✅5 🔹5 ⬜2 | R-03 路由评测无评测集；R-06 审批恒放行+补偿空壳；R-08 成本估算零命中+健康检查死字段；R-10 无等待审批/人工接管；R-11 无游标+无备份；R-12 账本整份缺失 |
| M 记忆上下文 | 10 | ✅6 🔹3 ⬜2 | ✅1 🔹7 ⬜2 | M-01..05 命名空间未接通（`memory_scope_key` 死字段）；M-08 冲突零检测；**M-09 上修为 ✅**（门禁真实且有测试） |
| C 专家能力与工作流 | 14 | ✅10 🔹2 ⬜2 | ✅6 🔹6 ⬜2 | C-03 `quality_metrics` 死字段；C-05 无打包实体；C-08 生命周期 3 值 vs 八态；C-09 零调用方；C-11 手工入口可零证据建候选 |
| E 观测诊断评测进化 | 13 | ✅12 🔹1 | ✅7 🔹5 ⬜1 | **E-07 候选生成零信号**；E-12 断链；E-13 并发/停止缺失；E-01 成本时延在自由字典且缺失静默计 0 |
| G 自主目标治理 | 10 | ⬜9 🔹1 | ⬜9 🔹1 | G-09 仅九级预算部分达成，缺预留/对账 |
| O 端云协同 | 10 | ✅7 🔹3 | ✅6 🔹3 ⬜1 | O-05 未发布时组件版本退化；**O-08 降为 ⬜**（无降级矩阵） |
| S 安全工程化 | 12 | ✅9 🔹3 | ✅4 🔹7 ⬜1 | S-02 隐私口径与契约不一致；S-03 仅正则黑名单且不覆盖模型输出；**S-05 执行环节零审计**；S-07 无 E2E 目录+自证式测试 |
| **合计** | **89** | **✅59 🔹15 ⬜15（66%）** | **✅34 🔹36 ⬜19（38%）** | — |

> **口径说明**：分组表实际 84 行 —— `P-01/02` 合 1 行覆盖 2 主题、`M-01..05` 合 1 行覆盖 5 主题，展开后为 89。
> 复核命令见 `IMPLEMENTATION.md` 文末。
>
> 核心结论不变：**名义 66% → 核验后 38%，落差约 28 个百分点**，
> 集中在"进化、记忆、契约、审计"四条主线。R 组与 S 组落差最大（12→5、9→4）。

### 2.2 五条主线

| 主线 | 现状 | 设计预期 | 缺口性质 |
|---|---|---|---|
| **A. 副作用与审批** | 无账本实体、无审批记录 | ADR-006 要求事件/检查点/副作用账本**三者分离**；R-12 六态 `planned/submitted/confirmed/failed/unknown/compensated`；A-01 审批绑定动作摘要+范围+有效期+一次性语义 | 整份实体缺失（可编码枚举已给） |
| **B. 进化闭环** | 候选只能手工创建；失败案例进不了数据集；进化侧无并发/停止控制 | E-07 四类信号自动提案；E-12 失败案例转回归资产；E-13 队列/预算/并发/停止四件套 | 自动信号与回归资产链**整段断裂** |
| **C. 契约层** | 3/10 契约整份缺失，17 字段形态不符，错误对象 0 字段 | 09 卷《核心接口与数据契约正式规范》 | 字段级可核对，**可立即开工** |
| **D. 资源与安全治理** | 无 CPU/内存/磁盘/电量维度；密钥无轮换；执行环节无审计 | S-10 四类设备×四种资源；S-11 四处禁止密钥落盘；S-05 七环节审计 | 设计与实现**同为原则层**，需先补设计 |
| **E. 自主目标 G 组** | 9/10 完全空白 | 05 卷 G 组 + 09 卷 Goal 契约 | **文档侧也无契约**——必须先补写设计 |

---

## 3. 名不副实的 ✅ —— 逐条带证据

### 3.1 E-07 进化候选生成（最严重）

- 判定：标 ✅「失败/成本异常/质量退化/能力缺口→候选提案」
- 实际：`evoagent/evolution/controller.py:152-180` `propose_candidate()` 六个参数全部必填，**无任何信号检测代码**。四类信号在 `evoagent/` 全目录**零匹配**（`grep "capability_gap|质量退化|成本异常"` → 0 命中）。
- 全部调用点均为人工/外部传入：`evoagent/cli.py::cmd_propose`、`evoagent/api/server.py`（API 手工入口）、`evoagent/evolution/controller.py::propose_candidate`（定义）（HTTP body 直传 `target_component`/`hypothesis`）。
- `register_problem()`（`:144-145`）向 deque 追加后**永不读取**——问题队列本身也是死的。
- 后果：PRD 46-54 的"从真实运行证据自动发现改进点"未实现；E-11 收益评估、E-09 门禁都建立在无自动候选来源之上。
- **▸ 演进（2026-10-22）**：上述指控已闭合——`SignalCollector` 订阅真实运行事件判定四类信号并登记票据，`run_signal_pipeline` 消费票据自动提案（`evoagent/evolution/signals.py::SignalCollector`、`evoagent/evolution/controller.py::run_signal_pipeline`）。本段保留为历史基线。

### 3.2 R-12 外部副作用账本

- 判定：标 ✅「提交/确认/失败/对账状态；unknown 不盲重试」
- 实际：**无账本实体、无持久化表**（`core/persistence.py` 30 张表无 side_effect 表）。最接近的 `Checkpoint.pending_side_effects`（`core/checkpoint.py:29`）是**恒空死字段**：
  - 唯一写入点 `controller.py:544`：`cp.pending_side_effects = extras.get("pending_side_effects", [])`
  - 唯一传 `extras` 的调用方 `controller.py:391` 只传 `budget` 与 `plan` → **该字段生产环境恒为 `[]`**
  - 恢复门禁 `evoagent/core/controller.py:584` 因此**恒不触发**
  - `tests/test_control.py:149` 是**测试自己塞值**才让该分支变绿 —— 自证式测试
- `ToolResult.side_effect_uncertain` 只是 bool，不是账本状态。
- 直接违反 ADR-006「事件、检查点与副作用账本分离」。
- **▸ 演进（2026-10-22）**：六态持久化账本已落地并接线（`evoagent/core/side_effects.py::SideEffectLedger`），`pending_side_effects` 改为由账本投影；本段保留为历史基线。

### 3.3 R-11 检查点与恢复

- 判定：标 ✅「节点状态/恢复游标；恢复前强制安全备份」
- 实际：
  - **无恢复游标**：`CheckpointManager`（`evoagent/core/checkpoint.py:45-61`）只有 `save`/`latest`，每次生成新 `checkpoint_id` 且只保留 latest，无法回到任意中间断点。
  - **无节点级检查点（ReAct 路径）**：`evoagent/core/controller.py::_move` 的 `self.checkpoint(task, target.value)` 只写 `task_state` + 硬编码 `next_candidate_actions=[{"action":"resume"}]`，不写 `completed_nodes`/`pending_nodes`。节点级快照由 `evoagent/workflows/engine.py::_snapshot` 保存。
  - **"恢复前强制安全备份"完全未接线**：`grep -c backup evoagent/core/controller.py` = **0**。`BackupManager.restore` 的 `safety_backup`（`evoagent/core/backup.py::safety_backup`）只服务数据库恢复，与任务恢复是两条独立路径。
  - 两个关键限定语（"恢复游标""强制安全备份"）在说明文字里都找不到对应代码。

### 3.4 R-10 人工接管与等待审批

- 判定：标 ✅「停止传播+审批等待+人工恢复；安全收尾」
- 实际：取消/暂停/恢复**真实现**（`evoagent/core/controller.py:555/571/662`、`:647` 重试构造，`evoagent/react/engine.py:76-81` 双 flag 传播正确）。
- 但：
  - **等待审批是空的**：`evoagent/react/engine.py:110-114` 收到 `request_user` 立即返回，`evoagent/core/controller.py` 的工作流结果分支把它转成一条失败 check → 任务判 **FAILED**。没有任何"挂起等用户"语义。
  - **人工接管无落点**：`WAITING_USER` 在 `api/server.py` **零命中**——用户没有任何接口可以提交澄清答案让任务继续跑。
  - **工作流任务连暂停都不支持**：`evoagent/core/controller.py` 的 `pause_task` 直接返回"工作流任务暂不支持暂停"，"停止传播"在工作流路径上是断的。
- 一句话：系统能"暂停-恢复"，但不能"问一句、等回答、再继续"。

### 3.5 R-06 审批节点与补偿

- 判定：标 ✅「依赖/条件/并发/重试/审批/检查点/补偿」
- 实际：SKIPPED ✅（`engine.py:92-106`）、并发 ✅（`:160-172`）、重试 ✅（`:180-206`）、检查点续跑 ✅（`:254-271`）。**审批与补偿在生产链路是断的**：
  - **审批永不阻塞**：`controller.py:424` 传 `check_op("workflow_node_confirm", …)`，但该 op 名**不在** `permissions.py:41-44` 的 `high_risk_ops` 名单内；`check_op`（`:64-67`）对不在名单的 op 直接 `return ALLOW`。API 路径下 `app.py:92` 注入的 `confirm_callback` 由宿主注入，服务端默认 `None` → 恒 ALLOW。（`cli.py:38` 有个仅 CLI 生效的回调，属测试便利）
  - **审批请求不落库、无 approve 接口、无恢复路径**：`engine.py:80` `if node.state != PENDING: continue` → 重跑时 `WAITING_AUTH` 节点被**永久跳过**，任务卡死。
  - **补偿是空壳**：`_try_compensate`（`:248-252`）用空参 `{}` 执行、`ctx` 形参未使用、异常全吞、只处理失败的单节点而不回滚已成功上游；内置漫剧声明的补偿动作 `fs_list`（`evoagent/workflows/builtin.py:59`：`compensation_actions={"compose": "fs_list"}`）是只读列目录，物理上不可能回滚视频合成。**零测试**。

### 3.6 R-08 工具契约：健康检查是死字段，成本估算完全缺失

- 判定：标 ✅「Schema/权限/风险/超时/幂等/成本契约」
- 实际：
  - **成本估算 100% 缺失**：`grep -n cost evoagent/tools/contract.py` 与 `registry.py` **均零命中**——契约无任何成本字段，执行前后也不做成本预估（成本只在 `core/budget.py` 模型调用层事后计量）。
  - **健康检查是"永远健康"的假实现**：`ToolContract.health_check`（`contract.py:24`）类型齐全、`ToolRegistry.health()`（`registry.py:234-239`）确实去调它——但 `grep -c health_check evoagent/tools/builtin.py` = **0**，7 个内置工具**无一设置该字段**，于是 `bool(hc()) if hc else True` 永远走 else 分支。监控面板 tools_health **永远全绿**。

### 3.7 M-01..05 记忆命名空间：字段是装饰性的

- 判定：标 ✅「五类记忆分型+命名空间+隐私级别」
- 实际：
  - `MemoryKind` 8 类 ✅、`Scope` 5 值（task/project/expert/model/shared）✅、独立表 ✅、隐私级别 ✅。
  - **但 `memory_scope_key` 是纯死字段**：定义 `experts/registry.py:22`，赋值 `experts/builtin.py:28/40/49/60`，**全仓零读取点**。`controller.py:352-357` 装配时传 `scope_key=task.task_id`、`:808` 写记忆时传 `scope_key=task.domain`——与专家的 `memory_scope_key`（如 `"coding"`）**语义不同、值不同**。
  - 更隐蔽：`store.py:248` 检索过滤条件含 `or not m.scope_key` → 空 key 直接放行，**即使接上也无隔离能力**。
  - `Scope` 枚举**无 `session` 值**（契约要求 user/project/domain/session/task 分层）。
  - `MemoryKind` 与 `Scope` 交叉不校验（可写 `kind=DOMAIN_KNOWLEDGE + scope=SHARED`）。

### 3.8 S-05 审计：七环节缺"执行"

- 判定：标 ✅「授权/执行/诊断/评测/发布/回滚全审计」
- 实际：`grep -c audit evoagent/core/controller.py` = **0**。所有状态流转、记忆沉淀、失败建案均不留审计痕。覆盖到的仅 API/CLI 边界（`api/server.py` 约 24 处、`cli.py` 4 处）。**高风险操作恰恰发生在执行路径上。**

### 3.9 C-09 能力依赖图与影响分析：9 行实现、零调用方

- 判定：标 ✅「impact_analysis 确定回归范围」
- 实际：`experts/registry.py:89-98` 共 9 行：
  - 只算**直接下游**（`:91-92`），**无传递闭包**——A→B→C 改 A 只报 B
  - `must_rerun_tests` 硬编码返回 `["capability_eval"]`，不按受影响能力类型/风险区分
  - `can_reuse_evidence` 是布尔翻转
  - **全代码库零调用方、零测试**（`grep -rn impact_analysis evoagent/ tests/` 只命中 def 本身）
- 依赖图本身也只有 `builtin.py:64-73` 手工写的 2 节点 1 边。
- **▸ 演进（2026-10-22）**：`impact_analysis` 已改为传递闭包 + 分层回归范围，并被候选创建与 `GET /impact` 消费（`evoagent/experts/registry.py::impact_analysis`、`evoagent/api/impact_api.py`）；依赖图数据仍偏少。本段保留为历史基线。

### 3.10 C-11 经验来源与晋升证据：存在可绕过的门禁

- 判定：标 ✅「成功/失败案例→候选；评测证据绑定」
- 实际：`evolution/diagnostics.py:529-558` `promote_to_candidate` 的证据门禁是真的（无证据直接 raise、模型假设须先过探针）。但：
  - **只覆盖失败证据**：`DiagnosticCase` 全部来自 `record_failure`（`:255`）。成功运行→进化候选的路径**不存在**（成功运行只产出**工作流**候选，不是进化候选）。
  - **手工入口无证据校验**：`api/server.py:1091` `evidence=body.get("evidence") or []` 与 `cli.py:232` 接受**调用方自述的任意字符串数组**，`propose_candidate` 原样存入，**不校验是否真实存在于运行记录中** —— 绕过 `promote_to_candidate` 即可零证据建候选。

### 3.11 E-12 失败案例转回归资产：最后一环断裂

- 判定：标 ✅「可复现案例库（cases API）」
- 实际：`record_failure` 自动建 `DiagnosticCase`（含 `version_manifest` + `evidence_snapshot`，信息量足够做可复现用例），`promote_to_candidate` 可升级——但**全仓无任何 `DiagnosticCase → EvalCase` 转换**（`grep "to_eval|to_dataset|regression_case"` 零匹配）。案例止步于 `diagnostic_cases` 表，**进不了 datasets**，"回归资产库"实际是只读诊断展示表。

### 3.12 C-03 领域专家隔离：独立评测是死字段

- 判定：标 ✅「独立配置/记忆/工具/评测」
- 实际：5 个专家、独立 `system_prompt` ✅、`tool_whitelist` ✅、`memory_scope_key`（但见 3.7，死字段）、`budget_class` ✅。
  - **`quality_metrics` 只被赋值、从不被读取**：`experts/registry.py:26` 定义，`builtin.py:18/30` 两处赋值，**零消费点**——没有任何按专家维度跑的评测集或指标计算。
  - **隔离维度不完整**：`comic_drama` 专家既无 `tool_whitelist` 也无 `quality_metrics`（`builtin.py:43-51`），与通用内核完全共用；`mobile_operation` 专家白名单里**没有任何设备控制工具**。

### 3.13 其余降级项（简要）

| 主题 | 判定 | 实际 |
|---|---|---|
| R-04 Planner 计划修订 | ✅→🔹 | `_risks_for`（`evoagent/planning/planner.py::_risks_for`）是按 intent 硬编码的风险文案，不是可执行恢复策略；**无 replan/revise**，`ExecutionPlan.adjust` 全库唯一调用方是 `evoagent/react/engine.py:232` 的"同工具连续失败≥3 次"追加固定模板步骤 |
| R-03 路由质量评测 | ✅→⬜ | **无任何路由评测集/指标**。`evolution/datasets.py` 两个内置集共 4 用例全为 `time_query`/`computation` 通用任务，**零个路由/分类评测用例**；无准确率/混淆矩阵/top-k/回退率/弃权率 |
| E-01 运行事件与 Trace | ✅→🔹 | 无 node 级字段；成本/时延/结果全在 `payload` 自由字典，`events.py:275-279` 靠 `payload.get("latency_s")` 取值，**缺失静默计 0 → 时延聚合被无声低估**，任何依赖它做门禁或收益判定的结论不可靠 |
| E-13 独立进化控制器 | ✅→🔹 | 预算池 ✅、次数上限 ✅；**并发控制缺失**（进化侧无 Semaphore）、**"重复失败即停止"不存在**（`react/engine.py:160` 的 `repeated_failure` 是单任务 ReAct 停止，与进化实验无关） |
| E-06 评测集版本/覆盖 | 🔹 | `datasets.py` 全文 47 行 4 用例；无注册/版本演进机制，无覆盖率台账 |
| O-05 Runtime Manifest | ✅→🔹 | 独立表 ✅、每次运行落一份 ✅；但 `_version_manifest()` 依赖 `releases.current_release_id`，未发布时返回 `{"version":"unreleased"}` → **组件版本退化为空** |
| O-08 离线降级与恢复 | 🔹→⬜ | `offline/policy.ts:8` `OFFLINE_INTENTS = ["time_query"]` —— **仅此一项**；`degrade/degrade_matrix/offline_capable` 在客户端仅命中测试断言，**无任何降级矩阵声明** |
| S-03 不可信边界 | ✅→🔹 | `memory/context.py:19-26` 13 条正则黑名单，仅覆盖记忆与执行结果两路输入，**不覆盖模型输出本身**；无归一化/编码变体/分级/审计告警 |
| S-12 备份迁移 | 🔹 | 备份 ✅（三库 zip + SHA-256 + integrity_check + 恢复前安全备份）；**迁移 ❌**——`FORMAT_VERSION` 只用于**拒绝**不兼容备份（`backup.py:167-168`），无迁移脚本、无升级路径 |
| C-13 AI 漫剧能力包 | 🔹 | 7 节点全 mock（`builtin.py:13-18` 全是 lambda 返回 f-string）；无素材资产化、无角色一致性、无 FFmpeg 合成（`mock_compose` 返回字符串不产文件）；`out_path` 形参声明后全函数体未使用，**成片不落盘**；`storyboard` 节点用 `calculate` 算 `6` 来"分镜" |
| S-07 自动化测试 | 🔹 | 数量已更新为 475/128；**无独立端到端/兼容性矩阵测试目录** |

### 3.14 主题外差距（跨主题 / 客户端 / 打包一致性）

以下 8 项不在 89 主题表内，但同样是设计与实现的实际差距，保留自上一版并补充本次核验结论：

| # | 差距 | 出处 | 现状 | 归属批次 |
|---|---|---|---|---|
| 1 | 视觉/OCR 手机操作（截屏/OCR/坐标点击） | PRD 64/68 | ⬜ 语音已实现、节点验证已实现；**不提供坐标注入**（安全边界，正确） | 客户端专项 |
| 2 | 端到端加密、多设备冲突合并 | PRD 72-76/86 | ⬜ | 治理收尾 |
| 3 | 发布分支可视化与远端推送自动化 | PRD 59-63 | ⬜ 本地 CLI 已有；不自动 push、不做冲突合并 | 治理收尾 |
| 4 | 能力包制品真实下载与装载（OTA 接线） | 补充 A §4/§5 | 🔹 清单/双槽位 ✅，**传输与装载未接**；服务端不存制品本体 | 待办 17 |
| 5 | 诊断附件客户端字节常驻（重启需重选文件） | 补充 C §6 | 🔹 服务端全链 ✅ | 客户端专项 |
| 6 | 契约同步补 `run_id`/`schema_version`/`payload_ref` 与统一错误对象 | 待办 16 | ⬜ 本次核验确认统一错误对象 **7 字段 0 实现** | **第一批**（提升优先级） |
| 7 | Goal 实体契约（`goal_type`/`parent_goal_id` 等 11 字段）无存储与 API | 契约 §7 + 待办 15 | ⬜ Task 侧引用字段 ✅（Session 29 落的契约前置） | 需先补 G 组设计 |
| 8 | MANIFEST.json 与 docs 目录脱敏对应（docx 移出后哈希不齐） | 打包清单 | 说明性差异 | 已在 `资料/README.md` 声明 |

> 配套使用：`chat/对账_89主题出处.md` 记录 89 主题在原始设计对话中的出处。该表列出「出处列缺少的主题」时，若同时实现为 ⬜/🔹，即为真正的**设计与实现双缺口**点。

---

## 4. 契约层差距（逐字段，可直接开工）

来源：`09_接口数据契约与实施验收/EvoAgent_核心接口与数据契约正式规范.md`

### 4.1 三份契约整份缺失

| 契约 | 规范字段 | 现状 |
|---|---|---|
| **Goal** | `goal_id`/`goal_type`(5值)/`parent_goal_id`/`success_criteria`/`authorization_ref`/`budget_ref`/`deadline`/`no_progress_policy_ref`/`status`(10值)/`evidence_refs` | 仅 `tasks.goal_id` 一个可空外键列（Session 29 补的契约前置）+ `GoalOrigin` 枚举。无 Goal 实体、无表 |
| **Side-effect Ledger** | `effect_id`/`task_id`/`run_id`/`tool_id`/`idempotency_key`/`effect_summary`/`status`(6值)/`external_receipt_ref`/`last_reconciled_at` | 全部缺失。六态枚举 `planned/submitted/confirmed/failed/unknown/compensated` **零匹配**（`compensated` 在 `workflows/models.py:54` 有对应动作但从不写回账本） |
| **Capability Asset** | `asset_id`/`asset_type`(8值)/`namespace`(5值)/`version`/`content_digest`/`source_refs`/`dependency_refs`/`compatibility`/`lifecycle_state`(8值)/`evaluation_evidence_refs`/`rollback_ref` | 只有 `CapabilityEntry`（`experts/registry.py::CapabilityEntry`）：`asset_type` 被 `domain` 取代、`lifecycle_state` 只 3 值 `active/experimental/disabled`、`namespace` 完全缺失 |

### 4.2 统一错误对象：7 字段 0 实现

规范原文：至少含 `error_code`、`message`、`retryable`、`correlation_id`、`related_event_id`、`required_action`、`safe_to_repeat`，且 **`safe_to_repeat=unknown` 时必须先对账或人工确认**。

实际：全 API 统一返回 `{"error": "中文可读串"}`（`api/server.py` 40+ 处 `self._json(NNN, {"error": ...})`）。7 字段零匹配，机器不可判定。
"unknown 先对账"这条规则目前只以散文形式落在 `controller.py:566-584` 的 `resume_task(force=True)` 与 `replay.py:86`。
**这是所有验收失败可定位性的地基，优先级应高于 G 组。**

### 4.3 其余契约的字段级缺口

| 契约 | 缺失/不符字段 |
|---|---|
| Task | `acceptance_contract_ref`、`authorization_ref`、`runtime_manifest_id`（实体上）、`idempotency_key`（Task 级，键散落三处且不同名：`client_request_id`/`idem_key`/`ToolResult.idempotency_key`）、`created_at`/`updated_at`（类型 float vs RFC3339）、`constraints`（类型 List[str] vs 对象）、`status`（缺 `waiting_approval`）、`objective`/`risk_class`/`budget_ref` 字段名不符 |
| Event | `schema_version`（全仓 py+ts 零匹配）、`payload_ref`（现为内联 `payload`）、`privacy_level`（枚举口径不同）、`sync_status` 缺 `acknowledged` |
| Tool | `risk_class`（声明式，现为运行时从 `side_effect_level` 推导，**声明与执行不同源**）、`offline_capable`、`requires_network`、`cost_model`、`contract_test_ref`（契约标 required） |
| Runtime Manifest | `runtime_adapter{name,version}`（现为字符串）、`model_profile_ref`、`router_policy_ref`、`context_policy_ref`、`expert_ref`、`evaluation_suite_ref`、`environment_digest`（sha256 vs 版本串）、`tool_versions[]`（现为字符串）、`workflow_ref`（类型不符） |
| 统一标识 | `node_run_id`（**完全缺失**，DAG 节点无执行实例 ID）、`approval_id`（**完全缺失**，无持久化授权记录） |
| 版本兼容声明 | 权限要求、数据 Schema、模型能力要求、离线支持 四项独立可校验字段缺失；"缺失兼容声明的制品不得进入稳定通道"未做字段级校验 |

---

## 5. 设计文档侧的差距（这是本次审计最重要的发现）

### 5.1 8 个专项卷 79 个主题，100% 是模板填空

每个主题固定 9 段，其中 **5 段在同一卷内逐字重复、与主题 ID 完全无关**：

```
**输入与输出契约**  输入：<卷级固定 6 项>。输出：<卷级固定 5 项>。
**标准处理流程**    <卷级固定 7 步链条>
**控制权与边界**    <卷级固定 1 句>
**异常与恢复**      <卷级固定 2 句>
**验收标准**        验收目标：<"设计目标与范围"原句复制>。
**实现约束与依赖**  本模块必须通过统一 task_id/run_id、版本清单、结构化事件和审计链关联运行；…
```

**唯一的信息增量只有两处**：`设计目标与范围`（一句话）+ `正式设计决策`（1-2 句）。

| 卷 | 主题数 | 模板填充 | 有具体契约 | 可直接编码的契约条目 |
|---|---|---|---|---|
| 01 任务入口与执行内核 | 12 | 12 (100%) | 0 | 18 条决策句 + **1 组枚举**（R-12 六值副作用状态） |
| 02 记忆上下文 | 10 | 10 (100%) | 0 | 9 条决策句（6 条重复）；ContextManifest 仅 5 要素名 |
| 03 专家能力与工作流 | 14 | 14 (100%) | 0 | 8 条决策句（7 条重复）；生命周期枚举**不在正文** |
| 04 观测诊断评测进化 | 13 | 13 (100%) | 0 | 7 条决策句 + **2 组分级**（L1-L5、E0-E3） |
| 06 产品部署与终端 | 8 | 8 (100%) | 0 | 7 条决策句；无 PoC 判据、无里程碑 |
| 07 端云协同 | 10 | 10 (100%) | 0 | 8 条决策句（4 条重复） |
| 08 安全工程化 | 12 | 12 (100%) | 0 | 8 条决策句；隐私分级/注入规则/资源阈值**全缺** |
| **专项卷合计** | **79** | **79 (100%)** | **0** | **65 条决策句 + 3 组枚举/分级** |
| 07 补充：Android 服务端协同与控制器 | 19 | 0 | **19 (100%)** | 19 实体定义 + 约 40 条可编码断言 |
| 10 术语表 + ADR 台账 | 28 术语 + 14 ADR | — | 全部 | 7 组缩写映射 + 14 条 ADR |

**关键判断**：正式版的"详细设计"程度 = **决策清单**，不是**契约规范**。全部高密度契约集中在 1 个补充设计文件（A-01..07 / SV-01..08 / CTRL-01..04）+ 09 卷契约规范 + ADR/术语表。

**这直接改变了 G 组的实施方式**：`IMPLEMENTATION.md` 待办 15 写的是"阶段 E（G 组整组）"，但 G 组**无法照文档实现**——05 卷 G-01..G-10 的字段名、接口名、状态枚举、数值阈值**全部为 0**。唯一可编码来源是 09 卷 §7 Goal 契约（11 字段）。**必须先补写 G 组的补充设计，再开工实现。**

### 5.2 契约密度与实现质量的强相关（本次审计的规律性发现）

| 文档是否给出明确二元判断 | 实现质量 |
|---|---|
| 同步幂等（O-02）、诊断包安全（O-04）、原子回滚（A-06）、记忆写入门禁（M-09）、权限 fail-closed（S-01） | **实现质量很高**——因为约束足够确定，代码写得干净且带测试 |
| 资源治理（S-10）、密钥轮换（S-11）、隐私分级（S-02）、ContextManifest 实体（M-07）、副作用账本（R-12）、审批实体（A-01） | **实现停在原则层或完全缺失**——因为文档没给字段，实现无从下手 |

结论：**"留空"和"写歪"是同一个原因**。第 3 节里那些"名不副实的 ✅"（副作用账本塞进检查点、审批无实体、L 级语义冲突），对应的都是文档把内容写成原则而非契约的地方。

### 5.3 文档内部冲突：需先裁决再改代码

| # | 冲突 | 甲方 | 乙方 |
|---|---|---|---|
| 1 | Goal 状态取值 | 05 卷散文 7 态（提出/运行/等待/暂停/阻塞/完成/过期） | 09 卷契约 10 值（多 `ready`/`review`/`cancelled`） |
| 2 | `privacy_level` 枚举 | 契约 4 值 `public/internal/sensitive/restricted` | 实现 4 值 `standard/sensitive/secret/private`（`core/privacy.py:6`）——**数量相同语义不同** |
| 3 | `sync_status` 枚举 | 契约 `pending/in_flight/acknowledged/quarantined` | 服务端 `local/synced`（`events.py:31`）+ 客户端 `pending/in_flight/quarantined`——**两端都缺 `acknowledged`** |
| 4 | L 级语义 | 04 卷 L1-L5 = 故障影响/归因层级 | 实现 L0-L4 = 候选变更风险等级（`evolution/controller.py:25`）——**同名不同义** |
| 5 | `idempotency` 枚举 | 契约 `idempotent/keyed/non_idempotent` | 实现 `none/key/state-check` |
| 6 | Asset namespace 五域 | 契约 `protected_core/shared_validated/domain_private/project/task` | 记忆 `Scope` 是 `task/project/expert/model/shared`——**两套命名空间体系并存** |

---

## 6. G 组（自主目标治理）专项

### 6.1 现状

- `core/task.py:64` 有 `goal_id: Optional[str]`，`core/persistence.py:32-35` `tasks` 表有可空列 + `list_tasks_by_goal()`（`:218`）——这是 Session 29 落的**契约前置**，不是 G 组本体。
- `core/task.py:25` 有 `GoalOrigin.OPPORTUNITY` 枚举，**无任何产生该值的代码路径**。
- `Task.parent_task_id` + `_build_retry()`（`core/controller.py:723`）只有 1 层父子关系。
- 全仓 `class Goal` 零命中。

### 6.2 G 组可实现契约来源

| 主题 | 05 卷字段名 | 05 卷接口名 | 05 卷状态枚举 | 05 卷数值阈值 | 09 卷补充 |
|---|---|---|---|---|---|
| G-01 目标注册表 | 0 | 0 | 0 | 0 | Goal 契约 11 字段 |
| G-02 机会发现 | 0 | 0 | 0 | 0 | — |
| G-03 价值排序 | 0 | 0 | 0 | 0 | — |
| G-04 自主等级 | 0 | 0 | 1（散文 5 级） | 0 | — |
| G-05 Goal 状态机 | 0 | 0 | 1（散文 7 态，与契约冲突） | 0 | 10 值枚举 |
| G-06 无进展停止 | 0 | 0 | 1（散文 5 触发器） | **0** | — |
| G-07 子目标治理 | 0 | 0 | 0 | **0** | — |
| G-08 前后台调度 | 0 | 0 | 0 | 0 | — |
| G-09 预算账本 | 0 | 0 | 0 | 0 | — |
| G-10 结果归因 | 0 | 0 | 0 | 0 | — |
| **合计** | **0** | **0** | **3 个散文枚举** | **0** | **11 字段** |

### 6.3 G-09 是唯一有实质基础的（G 组其余全靠 `core/budget.py` 九级预算）

已具备：九级作用域 + `PoolLimit` 池 + 预算耗尽拒绝。
缺：**执行前预留（无 `reserve()`）**、**执行后对账（无 `reconcile()`）**、CPU/磁盘/并发维度记账、"未知费用按保守估计"。

### 6.4 系统级验收用例 19 现状

> 验收用例 19「主动目标达到无进展阈值后停止」——**无任何依赖组件**，只有单任务 ReAct 的 `no_progress`（判据是同一动作签名连续 3 次）。**目标级无进展检测与停止控制器不存在**。
> 其余 19 条用例（幂等创建/权限拒绝/Schema 阻断/未知副作用对账/重启一致/取消/预算/验收门/MANIFEST 还原/断网落地/幂等同步/恶意导入隔离/门禁不动指针/签名兼容/健康回滚/依赖图回归范围/路由回退/压缩保约束/晋级报告完整）依赖的能力里，第 3 节已指出**用例 4（副作用对账）**、**用例 16（依赖图回归范围）**、**用例 17（路由回退）**的支撑实现分别是死字段、零调用方、零评测集——**这三条用例的"通过"同样存疑**。

---

## 7. 建议的补齐顺序

### 第一批｜可立即开工（契约已给，1-2 周）

> **进度注记（2026-10-22）**：本批 1/2/3/6 已由并行工作流落地，
> 4 的子步①（`health_check`）已落地。剩余项见 `FEATURE_SPLIT.md` 特性表与 `ROADMAP.md` 轨道 T0/T6。

| 序 | 项 | 依据 | 验收信号 | 状态 |
|---|---|---|---|---|
| 1 | **统一错误对象 7 字段** | 09 卷 §10 | 全 API 返回含 `error_code`/`retryable`/`safe_to_repeat` 等；`safe_to_repeat=unknown` 强制走对账 | ✅ **已完成**（`e2681ff`） |
| 2 | **Side-effect Ledger 实体 + 六态状态机** | 09 卷 §8 + 01 卷 R-12 枚举 | 新表 + 状态机；`pending_side_effects` 由账本投影而来而非检查点自持 | ✅ **已完成**（`ad5709e`） |
| 3 | **审批记录实体（`approval_id`）** | 09 卷 §1 + A-01 五项 | 绑定动作摘要/目标对象/范围/有效期/**一次性语义**；`resume` 校验 `safe_to_repeat` | ✅ **已完成**（`084d71d`） |
| 4 | **修复 4 处死字段/零调用方** | 本报告 3.6/3.7/3.8/3.9 | `health_check` 有真实实现或从契约删除；`memory_scope_key` 真正参与检索隔离；执行环节落审计；`impact_analysis` 加传递闭包并被回归范围调用 | 🔶 **子步①完成**（`ae54291`：`health_check` 真探针 + 三值语义）；②③④在途 |
| 5 | **补齐 Event/Tool/Manifest 17 个字段** | 09 卷 §3/§4/§5 | 含 `schema_version`、`node_run_id`、`offline_capable`、`cost_model`、Manifest 的 `expert_ref`/`router_policy_ref` 等 | ⬜ 未开始（F-02/F-09/F-10/F-11） |
| 6 | **修正暂停检查点竞态** | 本报告 §1.3 | `controller.py:391/795` 检查点有序化，`latest()` 不被后写 RUNNING 覆盖 | ✅ **已完成**（`2c0515d`） |

### 第二批｜需先补设计再实现（1-2 周设计 + 2-3 周实现）

| 序 | 项 | 前置 |
|---|---|---|
| 7 | **E-07 进化候选自动生成** | 需补写 E-07 的四类信号定义（触发条件、输入、判据、产出形态）——04 卷只给了 4 个信号名称 |
| 8 | **E-12 失败案例 → 回归资产** | 需定义 `DiagnosticCase → EvalCase` 转换规则（哪些字段进用例、哪些进标签、如何脱敏） |
| 9 | **E-13 进化并发与停止条件** | 需定义进化侧并发上限与"重复失败停止"阈值 |
| 10 | **G 组补充设计** | **必须先补写 G-01..G-10 的字段/接口/状态机/阈值**（可从 09 卷 Goal 契约 + 05 卷散文决策句展开），并裁决 5.3 的 6 条冲突 |
| 11 | **R-10 人工接管 / 等待审批** | 需定义挂起态语义与澄清接口形态（当前 `WAITING_USER` 无 API） |
| 12 | **R-06 审批节点修复** | 需把 `workflow_node_confirm` 纳入高风险判定 + 定义审批落库与恢复路径 |

### 第三批｜长期（阶段 H 与治理收尾）

C-10 能力去重与别名合并、C-12 编程专家 AST/LSP、P-04 Windows 桌面端、P-05 Mini PC/Linux 宿主产品化、P-08 真实 Harness PoC（ADR-010 唯一非"已确认"状态）、能力包制品下载与装载、S-06 ADR 状态/取代链、S-10 低资源调度、S-11 密钥轮换、S-12 迁移台账与故障演练、A-05 端侧记忆/工作流缓存、O-08 降级矩阵、O-09 大资产清理、C-13 素材资产化。

---

## 8. 建议同步修改的既有文档

| 文档 | 建议动作 |
|---|---|
| `IMPLEMENTATION.md` 第 156-329 行 | 按本报告 §2.1 修正 ✅/🔹/⬜ 判定；在表头加"判定依据=源码核验，非文档转述"；补充 §3 的死字段清单 |
| `IMPLEMENTATION.md` §迭代待办 15/16 | 按"可立即开工 / 需先补设计"重新分层；G 组待办补"先补写 G 组契约"前置 |
| `FEATURES.md` §8 测试矩阵 | 数字已从 352/120 更新为 475/128 |
| `FEATURES.md` §9 未实现清单 | 补入统一错误对象、Side-effect Ledger、Goal 实体、Capability Asset 契约、审批记录、ContextManifest 实体 |
| `README.md` 末段 | 「已实现 / 迭代占位」段落加一句指向本报告，并修正测试数字 |
| `progress.md` | 追加本次审计 Session（Session 30），记录死字段清单与竞态缺陷 |
| `docs/` 补充设计 | 新增《自主目标治理补充设计》与《进化信号与回归资产补充设计》两份，填补 5.1 的契约空白 |

---

## 附录 A：核验方法

- **逐主题源码核验**：对 89 主题中 41 项 ✅ 逐条查实现位置、调用方与测试，而非依赖既有表格转述。
- **死字段检测**：`grep` 字段定义点 + 赋值点 + **读取点**，三者缺一即判死字段。本次命中 3 处（`health_check` 的实现侧、`quality_metrics`、`memory_scope_key`）。
- **零调用方检测**：`grep -rn` 全仓（含 `tests/`）确认函数被调用。
- **恒真分支检测**：对 `if hc() if hc else True` 一类短路分支，核查实际数据是否供给。
- **契约密度普查**：对 8 个专项卷做段落级逐字比对，统计模板段落出现次数与唯一信息增量。
- **实测基线**：`pytest tests/ -q`（**475**）+ 客户端 `node --test`（128），失败用例隔离重复 3 次判定确定性。

## 附录 B：可复现命令

```bash
cd E:/evalapp
python -m pytest tests/ -q                                   # 475 passed
cd android-app && node --test "test/offline/*.test.ts" "test/device/*.test.ts" "test/update/*.test.ts"

# 死字段自检（三条应只命中定义/赋值，不命中读取）
grep -rn "memory_scope_key" evoagent/ | grep -v pyc
grep -rn "quality_metrics"  evoagent/ | grep -v pyc
grep -c  "health_check"     evoagent/tools/builtin.py        # → 0

# 零调用方自检
grep -rn "impact_analysis" evoagent/ tests/ | grep -v pyc     # → 只有 def

# 副作用账本死字段自检
grep -n "pending_side_effects" evoagent/core/controller.py   # → 544 写、579 读；391 的 extras 不含该键

# 审批永不阻塞自检
grep -n "high_risk_ops" -A4 evoagent/core/permissions.py     # → 名单无 workflow_node_confirm
grep -rn "workflow_node_confirm" evoagent/                   # → 仅 controller:424 与 cli:38

# 执行环节无审计自检
grep -c "audit" evoagent/core/controller.py                  # → 0
```
