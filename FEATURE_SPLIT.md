# EvoAgent 特性拆分（协作开发并行版）

**制定日期**：2026-10-21
**配套**：`ROADMAP.md`（43 个工作包，按优先级排序）｜`GAP_ANALYSIS.md`（现状依据）
**目的**：把 ROADMAP 的工作包重组为**可独立开发、独立验收、独立合并**的特性，并给出协作协议。

---

## 1. 为什么 ROADMAP 的 43 个 WP 不能直接并行

并行开发的真实障碍不是任务量，而是**共享文件的写冲突**。实测耦合结果：

### 1.1 三个必然冲突的热点

| 文件 | 规模 | 冲突原因 | 涉及 WP 数 |
|---|---|---|---|
| `evoagent/core/persistence.py` | **1125 行**，其中 `_init_schema` 占 22-194 行 | **全部 30 张表的 `CREATE TABLE` 写死在同一个方法里** | 12+ |
| `evoagent/api/server.py` | **1500 行**，209 处 `self._json` | 路由是 `do_GET`/`do_POST` 里的**顺序 `if path ==` 链** | 8+ |
| `evoagent/core/controller.py` | **855 行** | 编排中枢，几乎所有执行侧改动都要动它 | 10+ |

**含义**：任何人要加一张表或一个端点，都要改 `persistence.py` 或 `server.py`。两个特性同时做 → 必然冲突解 → 串行化。这不是流程能解决的，是结构决定的。

### 1.2 高扇入的共享契约文件（改动需谨慎但冲突可控）

| 文件 | 被 import 次数 | 风险 |
|---|---|---|
| `core/task.py` / `core/events.py` / `core/budget.py` | 各 8 | 改字段会波及所有 import 方 |
| `tools/contract.py` | 7 | 契约补齐要改 |

---

## 2. 拆分策略

四条原则：

1. **新文件优先**：新特性默认新建模块，不改共享文件。
2. **热点单点化**（F-00 一次性重构）：把 `persistence.py` 的建表段和 `server.py` 的路由链改成**注册式**，之后加表/加端点只需新建文件。
3. **契约单一来源**：新建 `core/contracts.py` 作为所有数据形状与枚举的**唯一权威**。所有特性从这里取，不各自定义 —— 这同时消灭了 6 条文档枚举冲突在代码层面的复发。
4. **模块所有权**：一个特性包独占一个模块目录，两个包不碰同一文件。

---

## 3. F-00 协作基座（**必须最先做，串行**）—— ✅ 已完成（2026-10-21）

> 状态：**已实现并验证**。改动为纯加法，存量端点行为零变化。

| # | 交付 | 消除的冲突源 | 状态 |
|---|---|---|---|
| F-00.1 | `evoagent/core/schema/` 包：**10 个域模块**（tasks/sync/diagnostics/evolution/release/tools/workflows/observability/memory/imports），各导出 `DOMAIN`+`TABLES`；通过 `pkgutil` **自动发现**，`persistence.py::_init_schema` 只遍历执行 | **加表不再动 `persistence.py`**（1125 → 950 行） | ✅ |
| F-00.2 | `evoagent/api/routes.py` 注册表 + `RouteContext`；`server.py` 在 `do_GET/do_POST/do_PUT` 各加**一行** `_dispatch(...)`，未命中走既有 `if` 链 | **加端点不再动 `server.py`** | ✅ |
| F-00.3 | `evoagent/core/contracts.py`：10 类契约字段与枚举唯一权威 + **6 条文档冲突的代码级裁定**（`CONFLICT_DECISIONS`） | **枚举口径分歧** | ✅ |
| F-00.4 | `evoagent/core/schema/migrations.py` 版本化迁移框架（`schema_migrations` 表、幂等、版本记录）；历史 ALTER 逻辑正式化为基线迁移 | 多方同时改 schema；同时补 S-12 骨架 | ✅ |

**实际做到的隔离程度（强于原计划）**：原计划是"新建域模块 + 在 `__init__.py` 加一行注册"。实现时改为 `pkgutil` 自动发现，**连注册都不用改** —— 新增特性加表是**纯新建一个文件，零处改动**。

**F-00 验收**：
- ✅ 全量测试 **462 passed**（358 原有 + F-00 的 39 + F-01 的 11 + 台账校验器 22）
- ✅ 新增一张演示表**不触碰** `persistence.py` 即可生效（`schema/goals.py` 演示模块）
- ✅ 新增一个演示端点**不触碰** `server.py` 即可生效（`tests/test_collab_base_api.py` 真实 HTTP 验证）
- ✅ 存量端点行为零变化（`/health`/`/version`/`/tasks`/POST 任务提交/404 均验证）

**实施中修掉的两个真实缺陷**（均由新增测试捕获，值得记入协作规范）：
1. **请求体被提前消费**（严重）：`_ctx()` 若急切解析 body，会把流读空，导致 `POST /diagnostics/import` 这类**直接从 `rfile` 读原始字节**的存量端点读到空数据 → ZIP 压缩比防护测试超时失败。修复：`RouteContext.body` 改为**惰性属性**（body_getter + 缓存）。已加回归测试锁定。
2. **处理器返回值语义**：曾按返回值判定"是否命中"，但处理器约定是 `ctx.json(...)` 直接写响应、返回 `None` → 会误回落。修复：`dispatch_*` 返回**是否命中**，不依赖处理器返回值。已加测试锁定。

---

## 4. 特性清单

> 「独占」= 新建文件，零冲突。「触碰」= 需改共享文件，**必须串行**或走 §5 协议。
> 波次内可完全并行；跨波次有依赖。

### 波次 1（8 个特性并行，最大并行度）

| 特性 | 内容 | ROADMAP 来源 | 独占 | 触碰 | 并行说明 |
|---|---|---|---|---|---|
| **F-01** 统一错误对象 ✅**已落地** | 七字段错误响应，`safe_to_repeat=unknown` 强制对账 | WP-1.1 | `core/errors.py`（实际落点） | `api/server.py`（`_json` 单点接入） | **✅ 2026-10-21 完成**（并行工作流 `e2681ff`）。原计划的 `api/errors.py` 改为 `core/errors.py`（契约层归位），测试 `tests/test_error_object.py` 11 项 |
| **F-02** 工具契约补齐 | `cost_model`/`risk_class`/`offline_capable`/`requires_network`/`contract_test_ref` + 真实 `health_check` 探针 | WP-0.5 | `tools/probes.py` | `tools/contract.py`、`tools/builtin.py` | 契约文件扇入 7，但改动是**加带默认值字段**，风险低 |
| **F-03** 副作用账本 | 六态状态机 + `side_effects` 表 + 检查点改账本投影 | WP-0.2 | `core/side_effects.py`、`core/schema/side_effects.py` | `core/controller.py`（副作用上报埋点） | ⚠️ **达成验收用例 4**，优先级最高 |
| **F-04** 执行审计 | 状态流转/记忆沉淀/建案/预算拒绝落审计 | WP-0.6 | `core/audit.py` 扩展 + `core/audit_hooks.py` | `core/controller.py`（埋点） | 与 F-03 同碰 controller → **建议与 F-03 合并或错开** |
| **F-05** 记忆治理包 | 命名空间隔离接通 + `memory_scope_key`/`quality_metrics` 清理 + ContextManifest 实体 + 冲突识别 + 删除传播 + 压缩分域 | WP-0.7 + WP-5.1..5.4 | `memory/context_manifest.py`、`memory/conflict.py` | `memory/store.py`、`memory/context.py`、`experts/registry.py` | **6 个 WP 合成 1 个包**——它们全碰 `memory/store.py`，拆开必冲突 |
| **F-06** 影响分析 | 传递闭包 + 风险分级 + 接入回归决策 | WP-0.8 | `experts/impact.py` | `experts/registry.py`（转发） | **达成验收用例 16**，轻量 |
| **F-07** 进化候选自动生成 | 四类信号检测器 + 问题队列消费 + 证据强绑定 | WP-3.1 | `evolution/signals.py` | `evolution/controller.py` | 补上「自主进化」的实际空壳，**独立价值最高** |
| **F-08** 失败案例转回归资产 | `DiagnosticCase → EvalCase` 转换 + 一键并入隐藏保留集 | WP-3.2 + WP-3.3 | `evolution/regression.py` | `evolution/datasets.py` | 与 F-07 同域但不同文件 → **建议错开或合并** |

**波次 1 收尾**：验收用例 4 ✅、16 ✅。名义覆盖 +4。

### 波次 2（6 个特性并行）

| 特性 | 内容 | ROADMAP 来源 | 独占 | 触碰 | 依赖 |
|---|---|---|---|---|---|
| **F-09** Event/Task 契约 | `schema_version`/`payload_ref`/`privacy_level`/`sync_status`；`node_run_id`/`acceptance_contract_ref`/`authorization_ref`/Task 级 `idempotency_key` | WP-1.2 + WP-1.3 | `core/contracts.py` 的消费侧适配 | `core/events.py`、`core/task.py`、`offline/types.ts` | F-00.3 |
| **F-10** Runtime Manifest 结构化 | 9 个结构化字段 + 修复未发布时版本退化 | WP-1.4 | `core/manifest.py` | `core/controller.py`（`_version_manifest`） | F-09 |
| **F-11** Capability Asset 实体 | 十字段 + 八态生命周期 | WP-1.5 | `assets/entity.py`、`assets/lifecycle.py`、`core/schema/assets.py` | `experts/registry.py` | F-00.1 |
| **F-12** 人工接管与审批节点 | `WAITING_USER` 真挂起 + 澄清接口 + 工作流审批节点链路修复 + 审批记录实体 | WP-0.3 + WP-0.4 | `api/routes/approval.py`、`core/approval.py` | `react/engine.py`、`core/permissions.py`、`workflows/engine.py` | F-01 |
| **F-13** 检查点有序化 🔶**部分完成** | 暂停语义不被 RUNNING 覆盖；恢复游标 | WP-0.1 | — | `core/checkpoint.py`、`core/controller.py` | **写入有序性已完成**（`2c0515d`）；**恢复游标仍未做**（`CheckpointManager` 仍只有 `save`/`latest`，取不到任意中间断点）|
| **F-14** 候选门禁与控制器分离 | 手工入口证据校验 + CTRL-03/04 职责拆分 | WP-3.4 | `evolution/gates.py` | `evolution/controller.py` | F-07 |

### 波次 2.5（**纯文档，完全并行，可从今天就开始**）

| 特性 | 内容 | ROADMAP 来源 | 备注 |
|---|---|---|---|
| **F-15** G 组补充设计 | 10 个主题的字段表/状态机转移表/接口清单/量化阈值 | WP-2.0 | **零代码冲突**，且是 F-16..F-24 的硬前置 → **最高杠杆** |
| **F-16** 文档冲突裁定 | 6 条冲突的正式裁定记录 | ROADMAP §8 | 与 F-00.3 呼应，产出直接喂给 `contracts.py` |

### 波次 3（G 组实现，8 个特性高度并行 —— 新建目录为主）

前置：F-15 补写设计完成 + F-15 产出契约。

| 特性 | 内容 | ROADMAP 来源 | 独占目录 |
|---|---|---|---|
| F-17 | Goal 实体与注册表（11 字段 + CRUD） | WP-2.1 | `goals/entity.py`、`goals/store.py` |
| F-18 | Goal 状态机（10 值 + 合法转移表 + 守卫） | WP-2.2 | `goals/state_machine.py` |
| F-19 | 自主等级与授权矩阵（五级，透传 Action Guard，父不得扩权子） | WP-2.3 | `goals/autonomy.py` |
| F-20 | 无进展停止控制器（五类触发器 + 阈值） | WP-2.4 | `goals/no_progress.py` — **达成用例 19** |
| F-21 | 递归子目标治理（深度/数量/环路/去重） | WP-2.5 | `goals/recursive.py` |
| F-22 | 机会发现与价值排序（六维加权） | WP-2.6 | `goals/opportunity.py` |
| F-23 | APRO 前后台调度 | WP-2.7 | `goals/scheduler.py` |
| F-24 | 预算预留与对账（`reserve`/`reconcile`） | WP-2.8 | `core/budget_reserve.py` |
| F-25 | 主动任务结果归因 | WP-2.9 | `goals/attribution.py` |

**F-17..F-25 全部新建 `evoagent/goals/` 目录 → 内部零冲突**，只在接 `controller.py` / `app.py` 接线时需要串行。这是全计划并行度最高的一段。

### 波次 4（P2 收口）

| 特性 | 内容 | ROADMAP 来源 | 并行性 |
|---|---|---|---|
| F-26 | 编程专家 AST/LSP | WP-4.1 | 新建 `experts/coding/` + `tools/ast.py`，独立 |
| F-27 | 漫剧能力包实装 | WP-4.2 | `workflows/comic/`，独立 |
| F-28 | 能力去重别名合并 | WP-4.3 | `assets/dedupe.py` |
| F-29 | 能力包制品装载 | WP-4.4 | `release/artifacts.py` |
| F-30 | Harness PoC | WP-4.5 | **独立调研型任务**，可全程并行 |
| F-31..F-42 | 治理收尾 12 项（密钥轮换 / 迁移 / ADR 链 / 资源调度 / 降级矩阵 / 端侧缓存 / 跨端…） | WP-5.5..5.12 | 多数独立新建，个别触碰 `release/manager.py` |

### Android 客户端（**与服务端完全并行，天然隔离**）

客户端按目录天然无冲突，且不与任何服务端特性争文件：

| 特性 | 内容 | 独占目录 |
|---|---|---|
| F-43 | 离线降级矩阵与能力声明 | `android-app/src/offline/degrade.ts` |
| F-44 | 端侧记忆/上下文与工作流缓存 | `android-app/src/offline/cache/` |
| F-45 | 视觉/OCR 操作 | `android-app/src/device/vision.ts` |
| F-46 | 端到端加密 / 多设备冲突合并 | `android-app/src/offline/crypto.ts` |
| F-47..F-50 | 治理收尾客户端侧 | — |

**唯一共享点**：`android-app/App.tsx`（UI 接线）与 `package.json`。约定：**由集成者在合并阶段统一接线，各特性只交模块不碰 App.tsx**。

---

## 5. 协作协议

### 5.1 共享文件写入权

| 文件 | 写入规则 |
|---|---|
| `core/persistence.py` | **F-00 已冻结**（950 行，建表段已移出）。任何人不得改；加表走 `core/schema/` 新建域模块 |
| `api/server.py` | **F-00 已冻结**（仅新增 3 处 `_dispatch` 调用）。加端点走 `api/routes.py` 装饰器 |
| `core/schema/` | **新建域模块即自动发现**，无需登记。新增域不冲突；改已有域模块才会冲突 |
| `core/contracts.py` | **单一 owner**。其他特性如需枚举变更，走 owner 合并，禁止各自加枚举 |
| `core/controller.py` | **串行队列**：改动排期，见 §5.3 |
| `core/task.py`/`core/events.py`/`core/budget.py` | 加字段优先；改动前在特性 README 声明影响面 |
| `tools/contract.py` | F-02 owner；后续特性只读不改 |
| `memory/store.py` | F-05 owner |
| `evolution/controller.py` | F-07 owner；F-08/F-14 走队列 |
| `App.tsx`（客户端） | 仅集成者改 |

### 5.2 分支与提交

- 分支：`feat/<特性号>-<简名>`，如 `feat/F-03-side-effects`
- 特性号即合并顺序依据，**不得跳号合入**
- 每个特性 PR 必带：变更的共享文件清单、契约字段清单、测试增量、`pytest` 结果

### 5.3 串行队列（`controller.py` 的预约制）

以下特性会碰 `core/controller.py`，**必须排队**，同一时刻只允许一个在途：

```
F-13(检查点) → F-03(副作用埋点) → F-04(审计埋点) → F-10(Manifest) → F-12(审批接线) → F-17..F-25(G组接线)
```

**规避手段**：这些特性应尽量把逻辑放进自己的新模块，`controller.py` 里只留 3-10 行接线。这样排队时间从"天"降到"分钟"。

> 落地建议：F-03/F-04 在新模块里做好埋点函数，`controller.py` 只加一行 `self.hooks.on_x(...)`。

### 5.4 定义完成（DoD）

每个特性合并前必须满足：
1. 新增代码在**独占文件**中；共享文件改动行数 ≤ 30 行且只做接线
2. 带测试；`pytest tests/ -q` **全绿**，且**连续 3 次**全绿
3. 契约字段来自 `core/contracts.py`，不在本地重复定义枚举
4. 不引入新的"接口在、逻辑空"：新增字段必须有**产生它的代码路径**（本次审计发现 18 项虚假 ✅ 的教训）

> ✅ **DoD 第 2 条已无例外**：原阻碍项 `tests/test_control.py::TestPauseResume::test_pause_then_resume_completes`
> 约 50% 概率失败（检查点写入无序、`latest()` 取到后写的 RUNNING 覆盖 paused），
> **已由 F-13 修复**（并行工作流 `2c0515d`：先落 paused 检查点再持久化 SUSPENDED 状态）。
> 修复后连续 3 次全量跑通过，**DoD「连续 3 次全绿」现为严格要求，无例外**。

### 5.5 F-00 沉淀的两条协作规范（实施中踩坑所得）

1. **不要在分发前急切读取请求体。** 注册表/中间件若提前解析 body 会消费掉 `rfile`，
   导致直接从流读原始字节的端点（`POST /diagnostics/import`）读到空数据。必须**惰性读取 + 缓存**。
2. **分派按"是否命中"判定，不按处理器返回值判定。** 处理器约定是调用 `ctx.json(...)`
   直接写响应、返回 `None`；按返回值判定会误回落到既有链路。

---

## 6. 并行度与排期建议

| 阶段 | 可同时进行 | 说明 |
|---|---|---|
| F-00 | **1 人**（串行） | 1-2 天。其余人可同时做 F-15/F-16（纯文档） |
| 波次 1 | **6-8 人** | F-03 与 F-04 建议合并或错开（同碰 controller） |
| 波次 2 | **5-6 人** | 视 `controller.py` 队列长度 |
| 波次 2.5 | **1-2 人**（纯文档） | F-15 是 F-17..F-25 的前置，越早越好 |
| 波次 3 | **6-8 人** | 新建 `goals/` 目录，并行度最高 |
| 波次 4 | **4-6 人** | P2 收口 |
| Android | **2-3 人** | 全程与服务端并行 |

**团队规模建议**：若 3-4 人，聚焦 F-00 → 波次 1 → 波次 2，G 组（波次 3）等 F-15 出来再启。
若 6+ 人，可让 F-15 从第一天就并行启动，G 组实现几乎不受主干进度影响。

---

## 7. 特性 ↔ ROADMAP ↔ 验收 映射

| 特性 | 覆盖的 ROADMAP WP | 关联主题 | 达成的系统级验收 |
|---|---|---|---|
| F-01 | WP-1.1 | 横切 | 3（增强可定位性） |
| F-02 | WP-0.5 | R-08/E-04/G-09 | — |
| F-03 | WP-0.2 | R-12 | **用例 4** ✅ |
| F-04 | WP-0.6 | S-05 | — |
| F-05 | WP-0.7 + 5.1-5.4 | M-01..10/C-03 | 17（路由回退）、18（压缩保约束） |
| F-06 | WP-0.8 | C-09 | **用例 16** ✅ |
| F-07 | WP-3.1 | E-07 | — |
| F-08 | WP-3.2 + 3.3 | E-12/E-13 | — |
| F-09 | WP-1.2 + 1.3 | 横切契约 | — |
| F-10 | WP-1.4 | O-05 | **用例 9** 名副其实 |
| F-11 | WP-1.5 | B/C-05/C-08 | — |
| F-12 | WP-0.3 + 0.4 | R-06/R-10/A-01 | **用例 2**、**用例 17** |
| F-13 | WP-0.1 | R-11 | — |
| F-14 | WP-3.4 | E-09/C-11 | — |
| F-15 | WP-2.0 | G 组前置 | — |
| F-17..F-25 | WP-2.1..2.9 | G-01..G-10 | **用例 19**（F-20）✅ |
| F-26..F-30 | WP-4.x | C-12/C-13/C-10/H | — |
| F-31..F-42 | WP-5.x | S/O/A/P | — |

**达成 20/20 的关键节点**：F-03（用例 4）→ F-06（用例 16）→ F-15+F-20（用例 19）。
**F-15**（纯文档、零冲突、是高杠杆前置）仍是最佳切入点；F-01、F-00、F-13 已由并行工作流完成。

---

## 8. 给协作方的三句话

1. **今天就能开工的**：F-06（影响分析）、F-15（补写 G 组设计）、F-30（Harness 调研）、F-02（工具契约补齐）——这四个都不碰热点文件或只需文档。（F-01 错误对象已由并行工作流完成）
2. **动手前先看 §5.1**：你要碰的共享文件在不在别人的写入权范围里。
3. **别造第二个空壳**：新增字段必须同时给出产生它的代码路径和一条走通该路径的测试。本次审计发现 18 项 ✅ 属于"接口在、逻辑空"，别把新的空壳写进去。