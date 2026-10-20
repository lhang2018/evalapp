# IMPLEMENTATION — PRD / 架构条目 ↔ 代码可追溯性映射

目标（PRD 114）：全量能力保留在统一规划与架构依赖图中；本文档记录每个能力域的实现状态。
状态说明：✅ 本轮可用（含测试）｜🔹 接口已留、实现待迭代｜⬜ 未开始（在迭代计划中）。

## 服务端核心（Python, `evoagent/`）

| PRD / Arch 条目 | 模块 | 状态 | 说明 |
|---|---|---|---|
| PRD 7 统一任务入口 / Arch 3.1 任务模型 | `core/task.py`, `core/controller.py` | ✅ | 统一任务实体、分类→规划→执行→验收 |
| PRD 8 意图识别与路由 | `experts/registry.py` route(); `planning/intent.py` | ✅ | 显式 hint 优先；模型 JSON 分类限定已注册意图；失败回退关键字启发式；低置信 + 高风险在控制器暂停 |
| PRD 9-10 领域专家/能力依赖图 | `experts/` | ✅ | 注册表 + 5 专家 + 依赖边 + impact_analysis |
| PRD 11-13 Planning/ReAct/工作流 | `planning/`, `react/`, `workflows/` | ✅ | 模型 JSON 计划（≤12 步/唯一 id/依赖存在/无环）失败可追溯回退模板；动态保留原目标、防循环（12.3）、固化工件（13.4 有验收门禁） |
| PRD 14-16 工具生命周期/契约/恢复 | `tools/` | ✅ | 契约校验、五层结果区分（15）、幂等键、错误分类 |
| PRD 17-20 Harness/模型管理 | `react/engine.py`, `models/` | ✅ | 适配器契约统一；模型档案/路由/迁移步骤表 |
| PRD 21-28 记忆系统 | `memory/store.py` | ✅ | 类型/归属/可信状态/写入流程/冲突替代/过期 |
| PRD 29-33 上下文治理 | `memory/context.py` | ✅ | 装配优先级、预算、压缩、丢失检查 |
| PRD 34-36 检查点/恢复/取消 | `core/checkpoint.py`, `core/controller.py` | ✅ | 恢复先验证副作用状态（35）；取消阻止后续动作 |
| PRD 37-40 运行日志/可观测 | `core/events.py` + SQLite | ✅ | 事件模型字段与 Arch 9.1 对齐；日志分层待细化 🔹 |
| PRD 41-45 诊断/故障分类 | `evolution/diagnostics.py`, `evolution/probes.py` | ✅ | 失败自动沉淀持久化案例；规则从证据生成 A/B/C/D 多假设（支持/反对证据+不确定性）；6 个固定探针有界无外网；假设显式升级候选。LLM 辅助假设生成待迭代 🔹 |
| PRD 46-50 自进化引擎 | `evolution/` | ✅ | 候选生命周期状态机与 Arch 10.4 完全一致；收益十维（50）中质量/成本/时延可用 |
| PRD 51-54 评测体系/门禁 | `evolution/evaluator.py` | ✅ | 五层架构中第 3/4 层实现；门禁一票否决 |
| PRD 55-58 成本控制 | `core/budget.py` | ✅ | 单任务硬预算多维；进化日预算字段就绪 🔹 |
| PRD 59-63 Git/版本/发布 | `release/manager.py`; git 基线 | ✅ | 清单、签名、验证、灰度接口、回滚；已建立 git 基线（main），自动分支/合并操作 ⬜ |
| PRD 64-71 Android | `android-app/` | 🔹 | RN 壳 + REST；无障碍/视觉/Intent 操作待迭代 |
| PRD 72-76 离线与双端 | — | ⬜ | 客户端本地队列（架构已定，待客户端迭代） |
| PRD 77-79 能力包/AI 漫剧 | `workflows/builtin.py` | ✅ | 漫剧 DAG 示例 + 服务适配器可替换（78.4） |
| PRD 80-82 风险分级与发布 | `evolution/controller.py` | ✅ | L0-L4 规则表（81）；发布后监测待接指标源 🔹 |
| PRD 83-88 权限/安全/隐私 | `core/permissions.py` | ✅ | 最小权限、高风险确认、外部内容信任边界原则落实在工具层 |
| PRD 89-90 界面 | `api/server.py` + `android-app/` | 🔹 | REST 已备 UI 数据；管理端（90.1-90.7）⬜ |
| PRD 91 数据实体 | `core/persistence.py`, `memory/store.py` | ✅ | 逻辑实体的核心子集 |
| PRD 92-99 非功能需求 | 各模块内 | ✅/🔹 | 稳定性→检查点；性能/成本计量在列 |
| PRD 106-113 验收标准 | `tests/` | ✅ | 75 项：任务执行/预算/安全/回滚/验收/诊断闭环/规划与意图分类 |
| PRD 115 实施顺序 | Phase 1-2 全部 | ✅ | 阶段三（进化闭环）部分完成 |

## 服务重启恢复记录

无（UI 集中在 `android-app/App.tsx`；API 使用逐步标准库）。

## 关键设计决策（Arch 22.1 架构决策记录）

| 决策 | 备选 | 理由 |
|---|---|---|
| 运行时同步单线程模型 | asyncio | 任务多为 IO/工具短时执行；简化并发/取消/检查点正确性（Arch 3.2 主动管理并发由 API 层线程承载） |
| SQLite 为主存储 | Postgres | 零依赖、单机部署起步；PRD 91 明确「不要求全部映射为独立物理表」 |
| Mock + OpenAI 兼容双适配器 | 单一实现 | 无 Key 时也能全链路演示与测试，符合 PRD 4.5 解耦 |
| 评测门禁一票否决安全项 | 加权平均 | Arch 11.5 明确禁止 |
| 高风险工具默认确认（confirm）而非拒绝 | allow/deny | 与 PRD 70/84 一致，默认策略安全；测试可用回调注入 |

## 迭代待办（下一轮候选）

1. 真实 LLM 联调（openai_compat/规划/分类已接通，需 Key 验证）
2. Android 侧：无障碍接入、语音、本地队列（PRD 68/72-76）
3. LLM 辅助诊断假设（当前规则+探针已闭环；模型不参与根因判定，PRD Arch 9.4）
4. 运行时沙箱（候选真实代码执行隔离，PRD 47.4/L3）
5. Web 管理端（PRD 90） — API 数据层已备
6. 向量检索嵌入（memory 并用关键词 bigram，PRD 25 相关性）
