# EvoAgent 实施进度日志

## Session 1 (2026-10-20)

### 背景
- 输入文档：PRD 1.0（28 部分，140+ 项能力）、Architecture 1.0（23 章）。
- 用户决策：Python 核心/完整功能先行/双模型适配器/Android 用 React 系框架。
- 规模结论：一次交付 = 服务端核心全链路可运行 + 存量子系统的可工作简化实现 + Android 骨架，后续迭代补强。

### 完成事项
- [x] 规划文件建立（task_plan / progress）
- [x] 环境确认：Python 3.10.11，无第三方依赖假设（全标准库实现；requests/pytest 可选）
- [x] 工程目录与配置
- [x] 核心内核（任务模型、状态机、预算、权限、事件、检查点、SQLite 持久化）
- [x] 运行时控制器 + ReAct + Planning
- [x] 模型层（Mock + OpenAI 兼容 + 路由 + 模型注册表）
- [x] 工具系统（契约/注册表/生命周期/6 个内置工具/副作用分级）
- [x] 记忆与上下文（SQLite 记忆库、可信状态、装配/压缩）
- [x] 专家注册表 + 首批 5 个专家配置
- [x] 工作流 DAG 引擎 + 漫剧示例
- [x] 验收器 + 评测集/门禁 + 进化候选生命周期
- [x] 发布管理（清单/门禁/签名/回滚）
- [x] REST API（stdlib http.server，JSON over HTTP）
- [x] CLI（demo/run/status）
- [x] 测试 21 项全部通过（unittest 风格，pytest 可跑）
- [x] Android React Native 骨架（App.tsx、API 客户端、manifest、构建说明）
- [x] IMPLEMENTATION.md 可追溯性映射

### 测试结果
- `python -m pytest tests/ -q` → 51 passed（状态机/预算/工具契约与权限/运行时/记忆/工作流/进化门禁/发布管理/API）
- demo 冒烟：`python -m evoagent.cli demo` 3 个 ReAct 任务 SUCCEEDED；`workflow` 漫剧 DAG SUCCEEDED
- 客户端 API 冒烟：/health、/tasks、/tasks/{id}/events、/cancel 全部通过 test_api.py

### 本轮修复的缺陷（全部有对应测试回归）
- memory_items 列数不匹配 + 补 superseded_by + 旧库自动重建
- tasks 表缺 created_at + 旧库自动重建
- execute() 在早期失败后未中止导致状态机冲突
- Verifier 空 criteria 默认拒绝 → 改默认 result_nonempty
- MockModel intent 解析含 domain 后缀失败
- 工具副作用登记与权限确认链（file_write 需确认）
- 上下文压缩保留硬约束；中文 bigram 检索
- API /tasks/{id}/events 路由解析；file_write 工作目录隔离

### 遗留/待迭代
- Android 侧需在 Android Studio / RN CLI 真机构建验证（当前环境无模拟器）
- openai_compat 适配器需真实 Key 联调
- 进化系统的候选生成目前是启发式 + 评测门禁，真实"隔离环境跑代码"需沙箱轮
- Web/管理端（PRD 90）未做，API 已留好界面数据接口
- 无障碍/语音/本地任务队列（PRD 64/68/72-76）待 Android 迭代轮

## Session 2 (2026-10-20)

### 完成事项
- [x] Git 基线（`.gitignore` + `main` 分支基线提交）
- [x] 诊断模型：`FaultCategory` A/B/C/D、案例/假设/实验数据结构、证据→多假设规则表
- [x] 失败自动诊断钩子：`_finalize` / 规划失败 / 控制器异常都会沉淀案例，`task.runtime_manifest` 记录 case_id
- [x] 固定探针 6 个（tool_health / calc_roundtrip / file_write_sandbox / state_machine_selfcheck / memory_roundtrip / context_constraint_retention），超时转不确定、无外部网络
- [x] 诊断持久化：SQLite 三表 + CRUD（案例/假设/实验）
- [x] 有界实验：每案 3 次、每日 50 次、单探针 10s；探针只调分不宣布根因
- [x] 假设→候选显式升级（`candidate_from_hypothesis`），风险 L1-L4 映射，候选仍需门禁/发布流程
- [x] REST 五个诊断端点 + CLI `diagnose [task_id] [--probe]`
- [x] 真实模型 Planning（JSON 计划校验、坏计划可追溯回退模板）
- [x] 新增意图分类器（hint 短路 / 模型 JSON / 启发式回退 / 低置信标记）
- [x] 测试 75 项全部通过（新增 24 项：诊断 13 + 规划/意图 11）

### 测试结果
- `python -m pytest tests/ -q` → 75 passed
- `python -m evoagent.cli demo` / `workflow` 行为不变
- CLI 诊断冒烟：失败任务 → 案例 2 条假设 → `--probe tool_health` 通过并降低环境假设分数

### 设计约束（本轮确立）
- 规则优先：假设由确定性规则从证据生成，模型不参与根因判定（Arch 9.4）
- 永远多假设：不允许单条"就是提示词问题"结论；保留支持/反对证据与不确定性
- 置信度是评分不是校准概率；上限 0.90
- 诊断不发布：候选需显式升级，发布仍走评测门禁与 L0-L4 规则
- 探针隔离：临时目录本地副作用，禁止外部网络

### 遗留/待迭代（更新）
- LLM 辅助诊断假设生成与实验建议（当前规则+探针闭环）
- 真实 Key 联调验证 Planning/意图分类 JSON 输出
- Android 真机构建、无障碍/语音/本地队列
- 候选代码隔离沙箱、Web 管理端、向量检索
