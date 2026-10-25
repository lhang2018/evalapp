# ALN-09-H 真实候选宿主、持久问题队列与进程隔离

任务：ALN-09-H / 真实候选宿主、持久问题队列与进程隔离
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`f7ccfcb936f4ed06dea26b84b44c01805d19dff0`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线；基线仍为 candidate，prepared_candidate `21899e6` 未发布）
分支与最终 commit：`feature/user-direction` / `435d12f7bbaeef021299c87bfa6f529feaeaff5f`（tree `ffb1b1c9ae28a2a67661126a5850c34340525a3f`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：
  - `EvolutionController.register_problem(ProblemTicket)`（含信号采集器 `evolution/signals.py` 自动登记的票据）；
  - `EvolutionController.run_signal_pipeline()` 消费信号型票据；
  - `IsolatedCandidateHost.run(asset_id@version)` 执行注册在资产库里的真实候选资产。
- 修改前：
  - 问题票据只存在 `EvolutionController.tickets` 这个内存 `deque`：进程退出即丢，多进程之间无法共享，也没有领取/接管语义（09-F 交付说明已明写「本卡不提供多进程租约或可并行的隔离宿主」）。
  - 候选执行面只有 `SandboxRunner.run_code/run_file`：只能执行裸代码字符串/单文件，无法执行资产库中的真实候选资产，也没有「执行的是哪个候选」的身份绑定。
  - `evolution/sandbox.py` 固定执行沙箱内 `candidate.py`，不支持多文件项目。
- 修改后：
  - 新增 `evoagent/evolution/queue.py::ProblemQueue`：票据落 SQLite（`evolution_problem_queue`），`enqueue` 幂等；`claim` 单条 UPDATE 原子领取（可指定 ticket 或按最早可领）；`renew/release/recover_stale/complete` 全套租约语义；`complete` 回填 `candidate_id` 并把票据标记 consumed。
  - 租约带 fencing（`lease_token` + `attempts`）：租约过期被接管后，旧持有者再 `complete/release` 抛 `LeaseLost`，僵尸进程不能覆盖新持有者结果。
  - `EvolutionController`：有 Database 时自动建队列，初始化先 `recover_stale()` 再装载 pending 票据（重启不丢证据）；`run_signal_pipeline` 在提案前领取租约、成功 `complete(candidate_id)`、失败 `release`（保留证据待下轮）；无 Database 时完全保持原纯内存行为。
  - 新增 `evoagent/evolution/host.py::IsolatedCandidateHost`：按精确引用（`AssetRef`/`asset_id@version`）从资产库读取真实候选资产，要求 `content = {"entry": ..., "files": {路径: 源码文本}}`，重新计算 `content_digest` 与登记摘要比对后，才在一次性沙箱目录执行；返回候选身份（asset_ref/content_digest/entry/file_count）+ 执行证据（status/exit_code/duration/violations/stdout/stderr）。
  - `evolution/sandbox.py`：新增 `run_project(files, entry, ...)`，复用同一超时强杀/网络/子进程/凭据边界；入口由 `EVOAGENT_SANDBOX_ENTRY` 指定并把沙箱目录加入导入路径（支持多文件候选互相 import）；路径必须是沙箱内相对路径（拒绝绝对路径、盘符、`..` 与越界写入）。

## 范围

- 独占模块：`evoagent/evolution/queue.py`、`evoagent/evolution/host.py`（新增）。
- 共享文件补丁与整合角色：
  - `evoagent/evolution/controller.py`：问题票据登记/消费接线（ALN-09-H 认领的 9 个共享文件之一）；
  - `evoagent/evolution/sandbox.py`：新增 `run_project`，未改 `run_code/run_file` 行为；
  - `evoagent/core/schema/evolution.py`：新增 `evolution_problem_queue` 表与索引（建表语句幂等，走域模块而非 `persistence._init_schema`，符合 `core/schema/migrations.py` 约定）。
  - 提交内未触碰 `api/`、客户端与工作流/预算/资产等其他角色共享文件；工作区里其他作者的未提交改动未被纳入本提交。
- 新增/修改契约，提供方与消费方：
  - 新增内部执行面约定：可执行候选资产必须提供 `content.files`（及可选 `content.entry`，默认 `candidate.py`）；非代码型资产（无 `files`）拒绝执行。
  - 队列行新增 `candidate_id`，供崩溃恢复后核对候选身份。
- 数据迁移、旧格式兼容或明确拒绝：
  - 新表由 `core/schema` 域模块在每次连接时 `CREATE TABLE IF NOT EXISTS` 自动创建，旧库无需迁移、不重建、不丢数据；未新增版本化迁移。
  - 旧行为（无 Database 的纯内存队列/无租约）保持不变，等价于「持久化关」。

## 验证证据

- 正常路径及实际产物：
  - 新测试 16 项全通过：`python -m pytest tests/test_evolution_queue_host.py -q` → `16 passed in 2.07s`（日志见提交内 `.planning/2026-10-25-aln09h/`）。
  - 覆盖：注册资产执行通过并回传 identity、多文件 sibling import、队列幂等入队、跨 Database 连接（模拟两进程）领取互斥、释放后重新可领、重启后 pending 票据可装载、控制器登记→提案→消费全链、重启后候选身份仍可解析。
- 拒绝/失败/重复/并发/重启：
  - 超时强杀：候选资产 `time.sleep(5)`，`timeout=0.5` → `status=timeout` 且 `duration_s < 3`；
  - 网络边界：资产内 `socket.getaddrinfo` → `status=violation`，violations 记录 socket 事件；
  - 凭据边界：宿主环境里的 `EVOAGENT_API_KEY` 不出现在候选进程环境/输出；
  - 崩溃接管：租约过期后另一个连接 `claim` 成功且 `attempts` 由 1→2；旧持有者 `complete/release` 抛 `LeaseLost`（fencing）；
  - 篡改拒绝：直接改写资产库 `content_json` 后宿主在执行前抛 `CandidateAssetError`；
  - 输入拒绝：无 `content.files` 的资产、入口不在 files 中、`../escape.py` 越界路径均拒绝（不执行）。
- 命令、结果、日志与对应源码 commit：
  - 提交 `435d12f7bbaeef021299c87bfa6f529feaeaff5f`；
  - 本卡专项：`python -m pytest tests/test_evolution_queue_host.py -q` → 16 passed；
  - 相关域回归：`tests/test_evolution.py tests/test_evolution_benefit.py tests/test_evolution_queue_host.py tests/test_sandbox.py tests/test_evo_signals.py tests/test_asset_consumption.py tests/test_asset_identity.py` → `138 passed in 11.93s`；
  - 全量后端：`python -m pytest tests/ -q` → **1769 passed / 2 failed / 1 skipped（375.12s）**，日志 `.planning/2026-10-25-aln09h/backend-tests.txt`；
  - 改动前基线（同一工作区、本人改动前）：`1751 passed / 1 failed / 1 skipped`，日志 `.planning/2026-10-25-aln09h/backend-baseline.txt`。
  - 全量中 2 项失败**均不是本卡引入**，已用「暂存本人 3 个改动文件后复跑」归因：
    - `tests/test_runtime_reservations.py::test_explicit_hint_does_not_allocate_a_phantom_classifier_call`：无本人改动时同样失败；相关文件 `evoagent/core/budget.py`(15:21)、`evoagent/core/controller.py`(15:21) 在本会话期间被他人在途改动，属预算/预留线（ALN-15-P/07-C 方向）未提交工作；
    - `tests/test_ledger_drift.py::TestNoDrift::test_all_ledgers_have_no_drift`：无本人改动时同样失败；`GAP_ANALYSIS.md:177` 的裸引用 `cli.py:232` 因他人 15:27 修改 `evoagent/cli.py` 而漂移（`tools/scan_all_ledgers.py` 报告 1 条 `bare_points_to_blank`），与本卡文件无关。
  - 改动前基线中唯一的 `test_goal_opportunities.py::test_real_runtime_manual_policy_runs_once_then_pauses` 失败单独复跑为 `1 passed`，为跨用例串行时的偶发，未计入本卡结论。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 本卡验证的是**本地资产库登记的真实候选资产**在受控沙箱中的执行与队列恢复，不是生产供应商制品、不是多机部署、不是真机；沙箱仍是降低风险的受控执行环境而非强安全边界（Arch 18.3 口径不变）。
  - 未接 API/前端（不在本卡写入边界；`api/` 属他人共享文件）；未做跨机时钟校准与分布式调度。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert 435d12f7bbaeef021299c87bfa6f529feaeaff5f`（新表 `evolution_problem_queue` 可保留：幂等建表、无数据迁移；控制器在没有队列/无库时自动回落纯内存行为）。回滚后需重跑 `tests/test_evolution_queue_host.py`（应整体消失）与相关域回归。
- 保留的限制、后续 ALN 和依赖：
  - 候选资产来源仍是本地资产库登记件；真实候选变更/制品的来源与收益证据属 ALN-06-R、ALN-10-E（本卡只提供执行与恢复通道）；
  - 提案与 `complete` 之间存在窗口：租约被接管时可能重复提案，当前靠组件级节流 `_has_open_candidate_for` 抑制，未实现「票据→候选」跨进程幂等映射（后续可挂 ALN-09 整包或 14/15 编排线）；
  - 租约 TTL 默认 15s，未做跨机时钟校准；多进程恢复用两个 Database 连接模拟，未做真实多进程/多机实测（需受控宿主环境，属 ALN-09 整包剩余范围）；
  - 未做候选资产执行的资源限额（CPU/内存/磁盘配额），仅超时与输出截断。
- 影响的主题/实体/ADR/契约/系统验收条目：PRD 47.4 / Arch 18.3 的执行边界沿用不变；新增 `evolution_problem_queue` 表与 `ProblemQueue`/`IsolatedCandidateHost` 两个内部接口；ALN-09 整包关闭仍需真实宿主、持久队列与进程隔离的生产级实测。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-09 真实候选宿主/持久队列/进程隔离仅本卡范围交付，整包与生产环境仍待验）
