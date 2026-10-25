# ALN-20-P 固定外部Runtime真实PoC

任务：ALN-20-P / 固定外部Runtime真实PoC
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`33529bef6b4bb54dc6c3a2d8385b8e0e36f38ea0`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `da066db5c25de88632a1407abe6b06a6283915d2`（tree `c148b4125f07b531ca88a2b752c46c57f42f5d56`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：需要判断「固定版本的外部 Agent Runtime / Harness 能否被本系统采用」——不是文档推演，而是用真实外部进程跑一次主链边界验证。
- 修改前：
  - 20-A 已交付 `RuntimeProtocol` 五类能力契约、`MockRuntime`、`ExistingRuntimeAdapter` 与能力兼容矩阵（含 `model_api_only` 参照行），但**没有任何真实外部 Runtime 的接入与裁决证据**；矩阵里也没有「外部 Runtime + 既有权威补位」这一实现行。
- 修改后（新增 `evoagent/runtime/external_poc.py`，矩阵新增一行）：
  - 被测外部 Runtime：**本机 Ollama 0.40.11 HTTP 服务 + 已下载本地模型**（真实外部进程，非夹具）。
  - `fetch_identity`：读 `/api/version` 与 `/api/tags`，绑定 **Runtime 版本号 + 模型内容摘要（digest）**；`verify_pin` 在版本或摘要漂移时抛 `RuntimePinMismatch`（fail-closed，拒绝执行、不自动升级）。
  - `ExternalRuntimeBridge`：实现 `RuntimeProtocol`，**五类能力全部委派既有权威**——工具→`ToolRegistry.execute`、事件→`EventBus`、取消→`controller.cancel_task`、恢复→`controller.recover_task`、沙箱→`SandboxRunner`；缺任一权威组件直接 `ValueError`（不静默降级、不复制第二套账本/Task 状态机）。
  - `generate()`：真实 `POST /api/generate` 执行（非流式），记录延迟/`eval_count`/`total_duration`；不可达或超时统一 `ExternalRuntimeUnavailable` 分类失败。
  - `poc()`：输出身份、矩阵一致性、真实生成、委派等价（事件往返/未知任务取消/沙箱真实执行）、失败隔离（不可达 + 版本漂移），并按 20-A 的 **AC-1..AC-4** 给出采用裁决与保留缺口。
  - `matrix.py` 新增 `external_ollama_http` 行（五类全 `full`，逐项注明补位组件），由 `assert_matches_declarations` 与运行时声明强制对齐。

## 真实 PoC 结果（本机实跑，非夹具）

| 项 | 结果 |
|---|---|
| 外部 Runtime | Ollama `0.40.11`（`/api/version` 实测） |
| 固定模型 | `ornith-1.5:9b`，digest `e5df7dcdd8a263994df62d610317e07be0d6af23f96fcbd8543273058fce575e`（`/api/tags` 实测） |
| 真实生成 | 成功；冷启动 `latency=8.504s` / `eval_count=41`；热重跑 `latency=1.015s`（同一提示） |
| 可复现性（AC-4） | 两次独立运行：`identity` 相同、`matrix` 相同、采用裁决同为 `adopt`（差异仅在耗时），见 `.planning/2026-10-25-aln20p/rerun-compare.json` |
| 委派等价 | 事件 `emit→subscribe→query_run` 往返一致；未知任务 `cancel_run` 返回 `ok=False ("任务不存在")`；沙箱真实执行 `print('external-runtime-poc')` 通过 |
| 失败隔离 | 不可达地址 → `ExternalRuntimeUnavailable`；版本漂移 → `RuntimePinMismatch`（且在真实生成前拒绝） |
| 采用裁决 | **adopt**：AC-1 五能力 full（补位）/ AC-2 权威未复制 / AC-3 语义可验证 / AC-4 版本+摘要固定可复现 |

## 范围

- 独占模块：`evoagent/runtime/external_poc.py`（新增）；`evoagent/runtime/matrix.py`（仅新增一行 `external_ollama_http`，未改 mock/existing/model_api_only 行与采用判据文本）。
- 共享文件补丁：**无**（本卡 `shared_files` 为空；未改 `protocol.py`/`mock.py`/`adapter.py`/`controller.py`/`app.py`）。
- 新增/修改契约：新增 `PinnedIdentity`、`RuntimePinMismatch`、`ExternalRuntimeUnavailable`、`generate()/poc()` 入口与矩阵行；五类能力语义不变（只是把实现从「本系统组件」换成「外部生成 + 本系统权威」）。
- 数据迁移/兼容：不涉及；不新增表、不改既有运行时行为；`external_ollama_http` 行只有显式使用才参与。

## 验证证据

- 正常路径及实际产物：真实生成成功、身份/矩阵/裁决证据见 `.planning/2026-10-25-aln20p/poc-evidence.json` 与 `poc-evidence-rerun.json`。
- 拒绝/失败/隔离：
  - 版本漂移、模型摘要漂移 → `RuntimePinMismatch`（在 `/api/generate` 之前拒绝，测试断言无 POST 发出）；
  - 不可达/超时 → `ExternalRuntimeUnavailable`，且事件/取消等既有权威仍可用（失败被隔离，不污染主链）；
  - 缺少权威组件构造 → `ValueError`（显式拒绝，不静默降级为「模型 API only」）。
- 命令、结果、日志与对应源码 commit：
  - 提交 `da066db5c25de88632a1407abe6b06a6283915d2`；
  - 本卡专项：`python -m pytest tests/test_external_runtime_poc.py -q` → **7 passed**；
  - runtime 域回归（runtime_protocol/runtime/peer_runtime_protocol_review/plan_runtime_review/runtime_router_rejection + 本卡）→ **67 passed**；本卡测试复用 20-A 的共享契约夹具 `run_contract_suite`（`assert_matches_declarations` + 五类能力语义）；
  - 真实 PoC 命令：`python -m evoagent.runtime.external_poc --base-url http://127.0.0.1:11434 --model ornith-1.5:9b --version 0.40.11 --digest e5df7dcd… --timeout 300 --out .planning/2026-10-25-aln20p/poc-evidence.json`（重跑输出 `poc-evidence-rerun.json`）；
  - 全量后端：`python -m pytest tests/ -q` → **1890 passed / 0 failed / 1 skipped（428.80s）**，日志 `.planning/2026-10-25-aln20p/backend-tests.txt`。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 本次是**本机真实外部进程**（Ollama）与本地模型的实跑，不是生产部署、不是远程供应商；模型推理在开发机执行；
  - 「真实主链采用」（任务路由到外部 Runtime 并由其驱动）**不在本卡范围**：本卡只证明采用判据与桥接边界；
  - 未测多并发/长上下文/大模型/多机时钟；外部 Runtime 无自主中断接管能力，取消与恢复语义仍由本系统 controller 承载。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert da066db5c25de88632a1407abe6b06a6283915d2`（删新模块/测试/矩阵行；既有 mock/existing 运行时与全部主链行为不变）。回滚后 `tests/test_external_runtime_poc.py` 整体消失。
- 保留的限制、后续 ALN 和依赖：
  - 真实主链采用（任务级路由、外部 Runtime 驱动工具循环、预算/授权在外部进程内的等价性）属 ALN-20 整包剩余范围；
  - 外部 Runtime 事件仅委派 EventBus，不把 Ollama 会话消息当 run 级账本（避免假证据）；
  - 未做并发/多机/长时间稳定性与成本实测；模型质量与生产供应商不可比；
  - 21-A（Windows/平板扩展）依赖本卡，现已可进入排期。
- 影响的主题/实体/ADR/契约/系统验收条目：能力矩阵新增「外部 Runtime + 权威补位」实现行；`RuntimeProtocol` 契约不变；ADR-010「模型 API 不能冒充 Runtime」仍成立（本卡用补位而非冒充）。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-20：真实主链采用与生产环境验证仍待；本卡交付固定版本外部 Runtime 的实跑 PoC 与采用裁决）
