# ALN-09-I 真实候选宿主消费与队列管理入口

> 2026-10-26 接续审查：修正release_before取值时序；评测期间独立发布如实显示差异。本轮未建立candidate_id/制品及数据集仓库权威绑定，不当发布授权。 本轮固定相关358项通过，修复提交0ba37e1/01766f5，新增修复待其他审阅人复核。下文修改前后及首轮验证是历史记录，最新行为以[复核报告](../验收/2026-10-26-新增宿主与音视频费用复核.md)为准；不提升整卡或生产接受。

任务：ALN-09-I / 真实候选宿主消费与队列管理入口（阶段4，assets 线）
负责人：dsh-lead（本会话认领；按"按优先级顺序认领"接 P1 READY 卡）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`1616e3e5e5f972023df88e1d9b60bc6cd5c9291c`（认领时 HEAD；基线 published `21899e6`）
最终 commit：`1d758e9adb762aa839b4c9c4ffb4379cb1bb1680`（tree `1dcb9cc447fd777f9e7cd28f9508dcd4544e595e`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 触发：09-H 已交付隔离候选宿主（`IsolatedCandidateHost`）与持久问题队列（`ProblemQueue`，已被进化控制器消费），10-E 已交付宿主基准（`HostBenchmark`）；但**没有受控的候选评测入口**，也没有 `ProblemQueue` 的查询/管理面——能力存在但零入口。
- 修改前：`evolution/host.py`、`evolution/queue.py`、`evaluation/host_bench.py` 只有库内能力与测试；无 API/服务装配。
- 修改后：
  1. 新增 [integration/candidate_execution.py](evoagent/integration/candidate_execution.py)：
     - `CandidateEvaluationService.evaluate()` 只接受**通过 `host.resolve` 内容摘要/版本/代码形态校验**的真实候选制品（未注册/篡改/非代码资产 → `CandidateAssetError`；空用例/非法参数 → 400）；结果绑定 `baseline/candidate` 身份（asset_ref/version/content_digest/entry）、`dataset_ref` 与实际宿主参数（benchmark_version/timeout/repeat/source），返回 `verdict/evidence_digest/release_decision`；
     - **评测成功不自动发布**：`auto_published=False` 且 `release_before == release_after`，发布仍由既有 release 门禁决定；
     - 队列管理：`queue_overview()`（只读 stats + pending 列表）与 `queue_recover()`（只调用 `ProblemQueue.recover_stale()`，只恢复**过期租约**，不动未过期/已消费票据）；领取/完成仍由进化控制器消费，不重建队列。
  2. 新增 [api/candidate_host_api.py](evoagent/api/candidate_host_api.py)：`GET /evolution/candidate-host/queue`、`POST .../queue/recover`、`POST .../evaluate`（均 admin；候选不存在 404、资产/基准不可执行 409、参数非法 400；operator 403）。
  3. `server.py` 一行 import；`app.py` 装配 `app.candidate_execution`（`AssetRepository(self.db._conn)` + `evolution.problem_queue` + `releases`）。

## 范围

- 独占模块：`evoagent/integration/candidate_execution.py`、`evoagent/api/candidate_host_api.py`（新增）、`tests/test_candidate_host_api.py`（新增）。
- 共享文件补丁（精确修改点）：`evoagent/app.py`（+7 行装配）、`evoagent/api/server.py`（一行 import）。
- 未改：`evolution/host.py`、`evolution/queue.py`、`evaluation/host_bench.py`（100% 复用）；未新增第二套任务/身份/预算/副作用权威。
- 新契约：评测服务结果结构、3 个 API 端点；无 schema 变更（`evolution_problem_queue` 沿用 09-H）。

## 验证证据（首轮历史记录）

- 专项：`python -m pytest tests/test_candidate_host_api.py -q` → **6 passed**：
  - 改进候选：verdict=improved、release_decision.allow=true，但**未自动发布**（release_before==release_after）；
  - 回退候选：verdict 非 improved、release_decision.allow=false 且 blockers 非空；
  - 超时候选：如实归因（release_decision.allow=false，不发布）；
  - 篡改/未注册/非代码资产、空用例 → 拒绝；
  - API：admin 评测成功、operator 403、未知候选 404、缺失候选资产 409；
  - 队列：pending 视图、过期租约恢复 1 张回到 pending、operator 403。
- 实测证据：`.planning/2026-10-26-aln09i/host-evaluation-evidence.json`，**8 项检查全过**（改进/回退/超时/拒绝 + 身份与宿主绑定 + 不自动发布）。
- 全量后端：`python -m pytest tests/ -q` → **2374 passed / 0 failed / 2 skipped（390.86s，复跑）**，日志 `.planning/2026-10-26-aln09i/backend-tests-rerun.txt`。首跑有 1 例 `test_attachments.py::TestAttachmentApi::test_chunk_size_and_index_validation` 报 `ConnectionAbortedError`（Windows 套接字中止），单测复跑通过、整包复跑全绿，判定为环境网络抖动而非本卡回归。
- 边界：
  - `dataset_ref` 是**调用方声明的数据集标签**，未校验数据集仓库是否存在/版本（避免伪造一个未验收的数据集权威）；
  - 队列管理为**只读视图 + 过期租约恢复**；手动领取/完成仍归进化控制器（防止绕过独立门禁）；
  - 未接 CLI/App 入口（卡片写入边界为 API）；未做真机/生产证据；
  - 评测服务复用主库连接（`AssetRepository(self.db._conn)`），并发压力未测（单实例 API 场景）。

## 回滚与未完成事项

- 回滚：`git revert 1d758e9adb762aa839b4c9c4ffb4379cb1bb1680`（删新增服务/端点/测试，撤销 app.py 装配与 server.py 一行 import；无 schema 变更）。
- 保留缺口：
  1. 数据集引用未做仓库级存在性/版本校验（需 10-C 数据集权威接入）；
  2. CLI 入口与 App 管理端未接（API 已可消费）；
  3. 队列手动重试/放弃等运维动作未开放（按"不绕过独立门禁"保留）；
  4. 多进程/高并发评测与真实生产证据未采。
- 影响：B-78/G2/G3 的候选宿主消费与队列管理入口交付；进化整包仍按父包跟踪。

## 审阅结论：待审

## 整包结论：仍实现中（数据集权威校验、CLI/App 入口、生产并发证据待补）
