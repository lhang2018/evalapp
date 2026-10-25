# ALN-10-E 真实候选收益与依赖回归证据

任务：ALN-10-E / 真实候选收益与依赖回归证据
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`89accb658a8ce5aa49c7e74d0e45904c2eda6b97`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `8f7625ed31f04ae21ff2bb01aee263baebe1da68`（tree `7f2610bfe89269ecf4d985bd8b11f1cfb38ce8b7`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：
  - 需要对一个真实候选资产（`asset_id@version`）给出「相对基线有没有收益、有没有依赖回归」的可复核证据；
  - 发布决策需要把该证据与发布后监测串成收益链。
- 修改前：
  - 10-H 已交付独立门禁与回归范围计算（`evolution/regression_gate.py`），09-H 已交付隔离候选宿主（真实资产 + 摘要校验 + 一次性沙箱执行）；但缺「把真实宿主上的候选对照跑成基准、给出覆盖/遗漏说明与发布前收益」的消费端，也没有发布前后收益链的证据形态。
- 修改后（新增 `evoagent/evaluation/host_bench.py` + 包 `__init__`）：
  - `HostBenchmark.observe/compare`：按精确 `asset_id@version`（含 `content_digest`）从资产库取基线/候选资产，**在 09-H 隔离宿主**中执行；用例输入写入沙箱 `input.json`，入口在 stdout 末行返回 JSON；`repeat` 次重复执行判定「对照可复现」。
  - 证据结构：baseline/candidate 身份（含摘要）、基准版本、`cases_digest`、逐用例结果（两侧状态/值/耗时/违规）、领域覆盖与**遗漏原因**（`timeout` / `violation` / `crashed` / `invalid_output` / `non_deterministic`）、成功率与耗时代价、`regression_cases`、`benefit`、`verdict`、`release_decision`、`evidence_digest`。
  - **fail-closed 裁决**：不可复现、必跑用例遗漏、覆盖率低于阈值、候选执行越界违规、候选由通过变失败（依赖回归）→ `release_decision.allow=False` 并列出阻断项；`verdict` 只可能是 `improved / regressed / neutral / inconclusive`。
  - `benefit_chain(report, post_release)`：把发布前对照与发布后监测串成收益链；缺发布后证据 → `status="pending"` 并注明「不得宣称线上收益」；发布后退化 → `rollback_required=True`（既定回滚触发点）。
  - `evidence_digest`：绑定资产身份/基准版本/逐用例结论/裁决，**刻意不绑定耗时等噪声字段**，使「同一资产、同一基准重复对照」可得到同一摘要（可核对）。

## 范围

- 独占模块：`evoagent/evaluation/__init__.py`、`evoagent/evaluation/host_bench.py`（新增包）。
- 共享文件补丁：**无**。本卡 shared_files 中登记的 `evolution/{controller,evaluator,regression,runner,candidates,monitor,benefit,routing_eval,experiment_budget}.py` 一行未改——复用 09-H 宿主与 10-H 门禁/回归范围，不重写已接受切片。
- 新增/修改契约：新增 `HostBenchmark` 与基准输入/输出协议（`input.json` + stdout 末行 JSON）；新增发布门禁输入 `release_decision` 与收益链 `benefit_chain` 形态；无数据库 schema 变更、无迁移。
- 数据迁移/兼容：不涉及；只有代码型候选资产（`content.files`）可参与基准，非代码资产由宿主拒绝（fail-closed）。

## 验证证据

- 正常路径及实际产物：
  - 改进候选（基线 2/5 通过 → 候选 5/5）：`verdict=improved`、`success_delta=0.6`、`coverage_ratio=1.0`、`release_decision.allow=True`；报告绑定基线/候选精确版本与 `content_digest`；同一资产重复对照得到**相同 `evidence_digest`**。
  - 发布前收益链在无发布后监测时 `status=pending`，不冒充已验证收益。
- 拒绝/失败/回归/恢复：
  - 回归候选（基线通过、候选失败 c2/c4）：`regression_cases=["c2","c4"]`、`verdict=regressed`、`release_decision.allow=False`（阻断项含「回归」）。
  - 非确定性候选（每次输出随机值）：`reproducible=False`、`verdict=inconclusive`、`allow=False`（阻断项含「对照不可复现」）。
  - 超时候选（sleep 5、基准 timeout 0.3）：遗漏原因 `candidate:timeout`、`required_coverage_ok=False`、`allow=False`（阻断项含「覆盖」）。
  - 越界候选（`socket.getaddrinfo`）：`metrics.violations>=1`、遗漏原因 `candidate:violation`、`allow=False`（阻断项含「越界」）。
  - 发布后监测：健康（success_rate 0.95 / 无回归）→ 收益链 complete 且不回滚；退化（0.2 或 regression=True）→ `rollback_required=True` 且给出 `rollback_reason`。
  - 身份绑定：候选换版本（1.1.0 → 1.1.1）后 `content_digest` 与 `evidence_digest` 均改变，`cases_digest` 不变（基准一致）。
  - 非法配置（`repeat=0`、`min_coverage=0`、空用例集）与非代码资产（knowledge）均被拒绝（`BenchError` / `CandidateAssetError`），不产出收益结论。
- 命令、结果、日志与对应源码 commit：
  - 提交 `8f7625ed31f04ae21ff2bb01aee263baebe1da68`；
  - 本卡专项：`python -m pytest tests/test_host_benchmark.py -q` → **8 passed**；
  - 本会话相关域回归（本卡 + 09-H 队列宿主 + 02-P 供应商 + 02-A 回执 + evolution 基础 + 13-P 调度）→ **73 passed（27.79s）**；
  - 全量后端（当前工作区，含其他作者已提交内容）：`python -m pytest tests/ -q` → **1842 passed / 1 failed / 1 skipped（442.54s）**，日志 `.planning/2026-10-25-aln10e/backend-tests.txt`；
  - 唯一失败 `tests/test_ledger_drift.py::TestNoDrift::test_all_ledgers_have_no_drift` 与本卡无关：`GAP_ANALYSIS.md:177` 的裸引用 `cli.py:232` 在 HEAD 已漂移（`tools/scan_all_ledgers.py` 报 `bare_points_to_blank`），该项在 02-P 交付时已在干净 HEAD worktree 上复现，本卡未改任何台账引用目标文件。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 基准是**本地受控基准**（真实资产库 + 真实隔离沙箱执行），不是生产流量、不是线上收益；报告显式带 `claim_scope="benchmark-only"`。
  - 未接 `release/manager` 与发布门禁的自动消费（本卡提供 `release_decision`/`benefit_chain` 输入契约，接线属发布线）；发布后监测数据需由 monitor 线提供，缺证据时链保持 pending。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert 8f7625ed31f04ae21ff2bb01aee263baebe1da68`（只删新增包与测试，无 schema/迁移/既有行为依赖）。回滚后 `tests/test_host_benchmark.py` 整体消失，09-H/10-H 既有能力不受影响。
- 保留的限制、后续 ALN 和依赖：
  - 基准用例集是本地代表性对照，未接入生产真实任务分布（属 ALN-08-P 生产评测集）；
  - 未接入发布门禁自动拦截与回滚执行器（`release/manager`、ALN-10-Q/R 已接受的门禁链由发布线接线）；
  - 耗时代价只做中位数与容忍度比较，未做统计显著性检验；
  - 基准进程内串行执行，未做并发/多机复现与宿主资源隔离压测。
- 影响的主题/实体/ADR/契约/系统验收条目：新增评测基准协议与发布门禁输入契约；不改变 09-H/10-H 语义；ALN-10 整包关闭仍需生产评测集与发布线接线。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-10：生产评测集、发布门禁自动拦截与真实收益回归链仍待验；本卡交付真实宿主对照基准与证据形态）
