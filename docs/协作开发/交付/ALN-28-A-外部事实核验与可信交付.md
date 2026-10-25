# ALN-28-A 外部事实核验与可信交付

任务：ALN-28-A / 外部事实核验与可信交付
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`1f2c0b0162e2681c338eeae033a47ab50212d44a`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `fbb6612414bf8dcb8d201d8ce8a4bf001ff95833`（tree `b268d923f27d7576f67d220e66e89d6e9dac4427`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：答案里出现「外部事实」（时间/版本/人物/事件等），需要**来源可访问、可复核**，并区分「有来源支持 / 来源矛盾 / 内部推断 / 未知 / 已过期」。
- 修改前：
  - 系统有记忆可信度枚举（candidate/verified/…）与 `http_get` 工具，但**没有外部事实核验层**：没有「来源记录（URL/发布者/获取时间/正文摘要/引用片段）」、没有交叉核验状态机、也没有「引用必须可访问才可交付」的交付规则。
- 修改后（新增 `evoagent/verification/` 包，不改既有模块）：
  - `sources.py`：`fetch_source` **真实抓取** HTTP(S) 来源并记录 URL、发布者、获取时间、正文 sha256、引用片段；抓取失败抛 `SourceFetchError`，**不伪造可用来源**；不可访问来源单独记录、不计入证据；`fixture_source` 仅用于本地对照并显式 `fixture=True`。
  - `claims.py`：`Claim` 带适用时间窗（`valid_from/valid_until`）、`model_generated`（默认模型自述）、`derived_from_internal`。
  - `crosscheck.py`：`FactCrossChecker` **fail-closed 五态**：适用时间已过 → `stale`（即使有来源支持）；有反对来源 → `contradicted`（支持/反对双方引用都可见）；有可访问来源直接支持 → `supported`（多发布者 0.9、单发布者 0.7）；仅提及不升级 → `unknown`；内部已核验事实推导 → `inferred`；其余 → `unknown` 且 `model_only=True`（**模型自述永不单独判可信**）。
  - `delivery.py`：`TrustedDelivery.build` 只把 `supported` 放进 `delivered`；`conflicts`/`unverified` 逐条可见；汇总引用（URL/摘要/片段，夹具带 `fixture` 标记）；`assert_deliverable` 防线拒绝「无引用却记 supported」「模型自述 supported」。
  - `report.py` CLI：真实抓取一个公网来源 + 五态夹具矩阵 + 真实引用交付 + 证据 JSON。

## 范围

- 独占模块：`evoagent/verification/`（新增包：`__init__/sources/claims/crosscheck/delivery/report`）。
- 共享文件补丁：**无**（未改 server.py/routes/memory/api；写边界在 `evoagent/verification/`，不需要动共享文件）。
- 新增/修改契约：新增来源记录、结论、核验状态、可信交付报告四类数据结构与规则项；无数据库 schema 变更、无迁移。
- 兼容：纯新增包，既有模块行为不变。

## 验证证据

- 正向（真实网络）：
  - `https://example.com/` 真实抓取成功：可达 `accessible=true`、延迟 0.317s、正文 sha256 `25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484`、发布者 `example.com`、引用片段 `Example Domain`；
  - 真实引用交付：结论「example.com 页面包含 “Example Domain” 文案」→ `supported`，`delivered=1`、引用 `fixture=false`、URL/摘要齐全。
- 五态矩阵（本地对照夹具，`fixture=true` 明确标注）：
  - `supported`（两发布者，confidence 0.9）；
  - `contradicted`（支持+反对双方可见）；
  - `inferred`（内部推导，未判 supported）；
  - `unknown`（无来源，`model_only=true`）；
  - `stale`（有来源但适用时间已过）；
  - `mentions` → `unknown`、`unreachable` → `unknown`（不可访问来源不计证据）。
- 拒绝/失败路径：抓取失败抛 `SourceFetchError`（不产生假来源）；不可访问来源只进 `inaccessible`；`assert_deliverable` 拒绝无引用/模型自述 supported（测试覆盖）。
- 命令、结果、日志与对应源码 commit：
  - 提交 `fbb6612414bf8dcb8d201d8ce8a4bf001ff95833`；
  - 本卡专项：`python -m pytest tests/test_fact_verification.py -q` → **11 passed, 1 skipped**（跳过的是 pytest 环境内公网用例；独立 `python -c` 真实抓取已成功，证据见 JSON）；
  - 实测命令：`python -m evoagent.verification.report --out .planning/2026-10-25-aln28a/verification-evidence.json` → exit 0，`verdict_ok=true`；
  - 全量后端：`python -m pytest tests/ -q` → **2040 passed / 0 failed / 2 skipped（434.71s）**，日志 `.planning/2026-10-25-aln28a/backend-tests.txt`（本次全绿，无他人未提交改动干扰）。
- Mock / 夹具 / 真实边界：
  - 真实网络：仅 `example.com` 一个稳定静态页（证明抓取、摘要、引用链路真实），**不是**业务事实数据源；
  - 五态矩阵使用**本地对照夹具**（标注 `fixture=True`），不冒充真实外部来源；
  - 未接外部可信数据源/搜索服务，未做多语言/多页面抓取；
  - 未接入任务主链（谁生成 Claim、何时要求核验属 24-A/28 后续接线）。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert fbb6612414bf8dcb8d201d8ce8a4bf001ff95833`（删新增包与测试；无 schema/迁移）。回滚后既有模块不受影响。
- 保留的限制、后续 ALN 和依赖：
  - 结论由调用方结构化提供；**自动从模型输出抽取 Claim** 未实现（属后续接线）；
  - 只抓取 HTML，未做 JS 渲染页/PDF/视频来源；
  - 未接真实事实数据源与缓存/时效刷新策略；
  - 未接 API/CLI 到任务主链，也未在 App 端展示（28-A 只交付核验与交付层）；
  - 抓取失败/不可访问只记录不计证据，未做重试与备用源选择。
- 影响的主题/实体/ADR/契约/系统验收条目：新增核验与可信交付契约；既有记忆/工具契约不变；A-61 的「来源/交叉核验/矛盾与时间窗」服务端部分交付。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-28：真实数据源、自动抽取与端到端接线仍待；本卡交付核验与可信交付层）


## 第三批审查修正（2026-10-25）

a1b3a3183b057b4e72a50677a703555a05ba4acf补齐正文引用匹配、原始字节摘要/来源大小上限、来源记录有效性、未来与非法时间窗及实际build交付守卫。真实抓取默认mentions；supports需要受信调用方明确核对，未实现自动语义判断。原夹具和公网采证保留为历史，不是新修复的外网验收。本轮网络相关探针为本地受控替身，公网用例显式不运行。

原独立复核读取到滚动工作区新字段而引用旧提交，新增修复需按上述精确提交独立复查。详见[第三批审查](../验收/2026-10-25-第三批交叉审查与真实性修复.md)。
