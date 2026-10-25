# ALN-23 任务契约对外模型（2026-10-25）

> owner：zcode-main。第一片实现 `0fda873`（三入口同源 + general 不短路）；
> 本文档是契约的权威描述。来源：qa141 十步用户旅程 §2/§3、PRD 7.3 任务
> 解析、任务卡 23-A。

## 契约字段（统一模型）

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| goal | string | 必填 | 自然语言目标 |
| intent | string | "general" | 意图提示；**general 不短路分类**（见下） |
| domain | string | "" | 领域提示 |
| constraints | string[] | [] | 明确约束（用户确认过的才收；任务全程必须遵守） |
| acceptance_criteria | {criterion:string}[] | [] | 验收条件（CLI 传文本包装为 criterion 条目） |
| target_object | string | "" | 目标对象（PRD 7.3） |
| expected_output | string | "" | 预期输出/目标状态 |
| complexity | enum | "" | simple/moderate/complex |
| required_tools | string[] | [] | 所需工具 |
| required_permissions | string[] | [] | 所需权限 |
| open_questions | string[] | [] | 尚未确定的关键信息 |
| requires_network | bool | null | 网络依赖 |
| workflow_suitable | bool | null | 工作流适用性 |
| risk_level | enum | "low" | 风险等级 |
| client_request_id | string | null | 幂等键（REST/客户端） |

## 三入口同源语义

| 入口 | 位置 | 字段覆盖 |
|---|---|---|
| REST | `POST /tasks`（server.py，`_parse_task_model_fields` 校验） | 全字段基准 |
| CLI | `evoagent task`（`--constraint/--acceptance/--required-tool/--required-permission/--workflow-suitable` + 既有字段） | 全字段（acceptance 文本包装为 criterion） |
| 客户端 | `TaskContractInput`（offline/types.ts）→ LocalTask.contract 持久化 → submit 链透传 → body 同名键 | 全字段（空值不发送） |

同源证明：`tests/test_task_contract_entries.py::test_cli_entry_same_fields`
（CLI vs REST 逐字段一致）；非法输入（complexity=ultra）三入口同拒。

## general 语义（不短路规则）

- `hint == "general"` **不**短路分类器：交给分类器确认或覆盖（关键词/模型），
  分类器回落仍是 general——语义不变，但真实分类不再被客户端兜底的
  general 压制（`controller._classify`，ALN-23-A）。
- 显式具体 intent 仍短路（confidence 1.0 "hint"）。

## 完成判定的对外口径（与 23-D 衔接）

- 任务详情 `GET /tasks/{id}` 返回 `to_dict()`，含全部契约字段与状态；
- 客户端展示层（features/taskview）只认服务端终态为完成，模型文字无效；
- 客户端提交（chat/工作模式）的种子约束经 `TaskContractInput.constraints`
  进入同一字段（23-B `buildTaskSeed` → submit）。

## 兼容与迁移

- 全部契约字段可选，旧客户端/旧调用不受影响；
- CLI 新参数均为 append/可选，无破坏；
- `LocalTask.contract` 为可选新增字段，旧持久化数据读取为 undefined。

## 测试映射

`tests/test_task_contract_entries.py`（7 项）：REST 全契约持久化、general
不屏蔽、显式短路保持、真 general 保持、非法 complexity 拒绝、CLI/REST
逐字段同源、cmd_task 契约注入。
