# EvoAgent — 自进化个人智能体平台（参考实现）

依据两份基线文档实现的可运行工程：

- `EvoAgent自进化智能体产.md` — 产品需求文档（PRD 1.0，28 部分）
- `EvoAgent自进化智能体系.md` — 架构设计文档（Architecture 1.0，23 章）

条目级代码映射见 [IMPLEMENTATION.md](./IMPLEMENTATION.md)。Android 客户端见 [android-app/](./android-app/)。

## 快速开始（无需 LLM Key）

```bash
# 全链路演示：意图→规划→ReAct→工具→验收→记忆
python -m evoagent.cli demo
# 工作流演示（AI 漫剧 DAG）
python -m evoagent.cli workflow
# 测试集
python -m pytest tests/ -q
# REST API（供 Android / 管理端）
python -m evoagent.cli serve --port 8300
# 直接提交任务
python -m evoagent.cli task "计算 2024+2000+500" --intent computation
```

## 接入真实 LLM（OpenAI 兼容：OpenAI/DeepSeek/Moonshot/vLLM…）

```powershell
$env:EVOAGENT_PROVIDER="openai"
$env:EVOAGENT_API_KEY="sk-..."           # 对应 config.provider
$env:EVOAGENT_BASE_URL="https://api.deepseek.com/v1"   # 可选，自定义兼容端点
$env:EVOAGENT_MODEL="deepseek-chat"
python -m evoagent.cli task "查询现在几点了" --intent time_query
```

Mock 与真实模型共用同一适配器契约；路由/档案/注册表逻辑一致（PRD 4.5 能力与模型解耦）。

## 架构总览（对应 Architecture 2.x 双轨）

```
Android 客户端 (RN/JS) ──REST──▶ ┌─ 稳定运行轨道 ─────────────────────┐
                                 │ CLI/API ─ RuntimeController        │
                                 │   ├─ PlanningEngine                │
                                 │   ├─ ReactEngine（模型建议+控制器执行）│
                                 │   ├─ WorkflowEngine（DAG）          │
                                 │   └─ 验收 Verifier                 │
                                 │ 模型路由 MockModel / OpenAICompat   │
                                 │ 工具注册表（契约/权限/预算/幂等）      │
                                 │ 记忆与上下文（SQLite）               │
                                 └────────────────────────────────────┘
                                 ┌─ 独立进化轨道 ─────────────────────┐
   运行事件 → 诊断案例 → 进化控制器 → 候选实验 → 评测门禁 → ReleaseManager │
   （签名制品 / 灰度 / 回滚）                                              │
                                 └────────────────────────────────────┘
```

## 安全边界（PRD 21 部分）

- 高风险工具（写文件等有副作用）默认 `confirm`：无确认回调即拒绝；`deny` 模式全拒。
- 硬预算由 `BudgetController` 程序化执行，模型无法绕过。
- 更新/发布须通过评测门禁与签名验证；核心安全边界改动禁止普通发布流程。

## 已实现 / 迭代占位

完整矩阵见 IMPLEMENTATION.md。当前一轮：服务端核心全链路可运行 + 测试通过（51 项）+
Android 骨架。迭代占位：无障碍操作、真实生成服务适配、向量检索、Web 管理端、隔离沙箱执行候选代码。

## 目录

```
evoagent/                  Python 服务端核心
  core/                    任务模型/状态机/控制器/预算/权限/检查点/事件/持久化
  planning/  react/        Planning 与 ReAct 引擎
  models/                  Mock + OpenAI 兼容适配器 + 路由 + 模型注册表
  tools/                   工具契约/注册表/内置工具
  memory/                  记忆存储与上下文装配
  experts/                 专家注册表（首批 5 个专家）
  workflows/               工作流 DAG 引擎 + 漫剧示例
  acceptance/              结果验收
  evolution/               评测集/门禁/进化候选生命周期/进化控制器
  release/                 发布清单/签名/回滚
  api/                     REST API（标准库实现）
  cli.py                   命令行
tests/                     51 项测试
android-app/               React Native Android 骨架
```
