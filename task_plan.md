# EvoAgent 实现计划

## 目标（Goal）
依据 `EvoAgent自进化智能体产.md`（PRD 1.0）与 `EvoAgent自进化智能体系.md`（Architecture 1.0）实现完整系统：
- **服务端核心（Python）**：真正可运行——统一任务模型、运行时控制器、ReAct、工具契约、检查点、预算、记忆/上下文、工作流、评测、进化候选、发布门禁、可观测性。
- **模型接入**：Mock 适配器（无 Key 可跑通全链路）+ OpenAI 兼容适配器（填 Key 即可用）。
- **Android 客户端（React/JS）**：React Native 工程骨架（含 WebView 化思路，便于后续 OTA 推送），交付源码 + 构建说明。
- **后续迭代**：在本 plan 中追加 Phase 处理优化项。

## 状态（Status）：in_progress

## Phases
| # | Phase | 内容 | Status |
|---|-------|------|--------|
| 1 | 工程基建 | 目录结构、config、planning 文件 | complete |
| 2 | 核心内核 | task.py / state_machine.py / budget.py / permissions.py / events.py / checkpoint.py / persistence | complete |
| 3 | 运行时 | controller.py（含恢复/取消/验收编排） | complete |
| 4 | 模型层 | base/mock/openai_compat/router + 模型注册表 | complete |
| 5 | 工具系统 | contract/registry+builtin 工具（含副作用等级、幂等键） | complete |
| 6 | ReAct | engine.py 循环 + 防循环机制 | complete |
| 7 | 规划 | planner.py + 规划验收 | complete |
| 8 | 记忆与上下文 | memory/store.py + memory/context.py | complete |
| 9 | 专家体系 | experts/ 注册表 + 首批专家配置 | complete |
| 10 | 工作流 | workflows/ DAG 引擎 + 漫剧示例流程 | complete |
| 11 | 验收与评测 | acceptance/verify.py + evolution/evaluator.py（评测集+门禁） | complete |
| 12 | 进化系统 | evolution/candidates.py + controller.py 候选生命周期 | complete |
| 13 | 发布管理 | release/manager.py（清单/门禁/签名/回滚） | complete |
| 14 | REST API | api/server.py（任务提交/查询/事件，供 Android 用） | complete |
| 15 | CLI + 演示 | cli.py + examples/demo.md | complete |
| 16 | 测试 | pytest：状态机/预算/ReAct/检查点恢复/工具契约/记忆/工作流/门禁/API | complete（51 passed） |
| 17 | Android 工程 | React Native 骨架 + API 客户端 + 构建说明 | complete |
| 18 | 可追溯性文档 | README + IMPLEMENTATION.md（PRD 条目↔代码映射） | complete |
| 19 | 迭代优化轮 | 真实模型联调、Android 真机构建、进化真实实验 | pending |

## 关键决策（Decisions）
| 决策 | 理由 |
|------|------|
| Python 同步执行模型（非 asyncio） | 可靠、易测试；架构文档不强制并发模型，控制器统一管理并发限制 |
| SQLite 持久化（标准库 sqlite3） | 零依赖、满足任务/检查点/事件/记忆持久化需求 |
| Mock 与 OpenAI 适配器同层 | 适配器契约统一（PRD 4.5 能力与模型解耦） |
| 影子执行的高风险工具 | PRD“计划完成≠实际完成”，file_write 等高风险工具需 confirm 回调 |
| Android 选用 React Native | 用户要求 React/JS + 后续 OTA 直接推送（Expo Updates/CodePush 路线） |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| pytest 未安装 | 1 | 保留 tests/，用 unittest 编写，pytest 兼容运行也可用 |

## 备注
- 无 LLM Key 时运行 `python -m evoagent.cli demo` 走 Mock 模型全链路。
- 有 Key 时设 `EVOAGENT_PROVIDER=openai` 与 `EVOAGENT_API_KEY`。
