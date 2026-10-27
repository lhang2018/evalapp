# ALN-24-F 多主体记忆范围策略与迁移隔离

> 2026-10-26 证据与策略接续复核：回退核对原legacy摘要和分区版本；迁移事务失败回滚、逐次追加回执、关闭分区不冒称迁移、actor取鉴权主体。真实多主体检索/上下文与UI消费仍未接。 修复093c561，固定相关341过、客户端新归档440过/类型检查；新增修复待其他审阅人复核。下文为原交付历史描述，最新行为及保留缺口见[复核报告](../验收/2026-10-26-证据消费与记忆策略接续复核.md)，不扩大接受。

任务：ALN-24-F / 多主体记忆范围策略与迁移隔离（P2，阶段4，integration 线；多主体开放前置）
负责人：dsh-lead（本会话认领；按"按优先级顺序认领"接 P0/P2 READY 卡）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`69b0524a6450fc14124a324f82c1a211b23cdb4b`（认领时 HEAD；基线 published `21899e6`）
最终 commit：`2e0c857473972ba2ab50bf3dd2c11ef465048fa2`（tree `ea54e67991c8e274754d9d144203ee65f92ac60e`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 触发：多主体开放前，需要把当前"本地单行策略"扩展为**受信主体分区**策略，并定义旧策略迁移与回退、租户隔离；同时不能重复 c6c9872 的检索过滤。
- 修改前：
  - `scope_policy.py` 只有 `read_scope_policy()`（单行表 `memory_scope_policy`，供 `store.py`/`user_control.py` 做检索与上下文过滤）；
  - 没有主体分区策略、没有迁移台账、没有"主体由服务端鉴权决定"的强制、也没有缺失/损坏策略的拒绝规则。
- 修改后（**保留 `read_scope_policy` 原语义与调用方不变**）：
  - `ScopePolicyStore`：
    - **主体由服务端鉴权决定**：`resolve_principal(authenticated, requested)` 在请求体/查询试图指定他人主体时抛 `PrincipalMismatch`；
    - **默认单用户不变**：未显式开启多主体时 `effective_policy()` 仍返回旧单行策略（source=legacy）；
    - **分区隔离**：`memory_principal_scope_policy` 按 `principal_id` 各存 allow/deny/version/actor/reason；同 scope 不同主体互不影响；
    - **缺失/损坏拒绝默认放宽**：`effective_policy(strict=True)` 对缺失/损坏旧策略抛错；非 strict 返回"拒绝全部范围 + 警告"（不跨主体开放）；
    - **迁移**：`migrate_legacy()` 支持 dry-run、幂等（同摘要→already_migrated）、冲突拒绝（目标已有不同策略）、旧策略缺失/损坏拒绝迁移；写 `memory_scope_policy_migrations` 台账；
    - **回退**：`rollback_migration()` 按台账 `new_digest` 核对当前分区策略（迁移后被篡改则拒绝），删除分区行并返回回退校验（source=legacy）；`migration_history()` 可查 dry_run/apply/rollback。
  - `api/memory_context_api.py` 追加：`GET /memory/principal-policy`、`PUT /memory/principal-policy`、`POST /memory/principal-policy/migrate`、`POST /memory/principal-policy/rollback`、`GET /memory/principal-policy/migrations`（写操作 admin 门限；请求主体与鉴权主体不一致 → 403）。

## 范围

- 独占模块：无新增文件；扩展 `evoagent/memory/scope_policy.py`（卡片写入边界内）。
- 共享文件补丁（精确修改点）：`evoagent/api/memory_context_api.py` —— 仅在文件末尾追加多主体端点与 import；既有记忆控制端点未改。
- 未改：`store.py`、`user_control.py`（继续使用 `read_scope_policy`，检索过滤语义不变）；未新增第二套检索/身份/预算权威。
- 兼容/迁移：幂等 `CREATE TABLE IF NOT EXISTS`（`memory_principal_scope_policy`、`memory_scope_policy_migrations`），无版本化迁移；单用户路径行为不变。

## 验证证据（原交付历史记录）

- 专项：`python -m pytest tests/test_multi_principal_scope.py -q` → **12 passed**：
  - 缺失/损坏旧策略拒绝默认开放；默认单用户仍走 legacy；
  - 双主体同 scope 隔离（allow/deny 独立、scope_visible 禁止优先）；
  - 请求体选他人主体 → `PrincipalMismatch`；非法 allow/deny/multi 标志拒绝；
  - 迁移 dry-run→apply→幂等，旧策略变化后冲突拒绝；回退成功且二次回退拒绝；迁移后篡改再回退拒绝；
  - 迁移历史含 dry_run/apply/rollback；文件库重开后分区策略持久；
  - HTTP：GET 返回 legacy；请求主体不一致 403；PUT/migrate/rollback/migrations admin 可用、operator 403。
- 相关域回归：`test_memory*.py + test_api.py + 本卡` → **91 passed（8.10s）**。
- 实测证据：`.planning/2026-10-26-aln24f/scope-policy-evidence.json`，**12 项检查全过**（双主体隔离、HTTP 迁移/回退、重启持久、operator 403）。
- 全量后端：`python -m pytest tests/ -q` → **2358 passed / 0 failed / 1 skipped（375.97s）**，日志 `.planning/2026-10-26-aln24f/backend-tests.txt`。
- 边界：
  - 检索/上下文装配的**过滤仍由既有 `read_scope_policy` 消费方执行**（不重复 c6c9872）；本卡只交付策略与迁移层；
  - 多主体默认关闭；HTTP 主体来自 `ctx.auth.principal_id`（当前部署 root→`api:admin`），未做真实多用户 auth 中间件联调；
  - App 端管理界面未接 principal-policy（30-B 只接了全局 scope-policy）；未做真机证据。

## 回滚与未完成事项

- 回滚：`git revert 2e0c857473972ba2ab50bf3dd2c11ef465048fa2`（删除新增策略类与端点；保留旧 `read_scope_policy`；新表为幂等建表，回滚不影响旧路径）。
- 保留缺口：
  1. 真实多主体鉴权联调（token 轮换/多用户）未做；
  2. 迁移只覆盖"旧单行策略→单主体分区"；多主体批量迁移/合并策略未做；
  3. App/UI 未提供 principal-policy 管理入口；
  4. 检索侧未新增分区过滤（按卡约束复用 c6c9872；但"分区策略→实际检索/上下文"端到端证据需在真实多主体场景补）；
  5. 真机/生产证据未采。
- 影响：ZG-02/ZG-10 的服务端策略与迁移前置部分交付；多主体开放仍需鉴权联调与检索端到端验证。

## 审阅结论：待审

## 整包结论：仍实现中（多主体开放前置：鉴权联调、检索端到端与 UI 管理仍待）

## 2026-10-27 指派接续审核

e89b4ae补齐初始化建表事务所有权/自身失败回滚，新9项组合探针修前8失败/1通过，聚焦77通过。固定相关487/1未选，客户端469/TS，具体范围见[十一项审核](../验收/2026-10-27-指定十一项接续审核.md)。新补丁待独立复核，原卡正式验收缺口与review保留。
