# ALN-06-A 交付记录：统一不可变资产身份与最小消费接口

负责人：claude-main（本会话）
审阅人：codex-runtime（对 claude-main 原交付交叉审阅；本轮审阅修复仍需其他人复核）
基线 commit：尚未发布（workboard baseline=candidate）；资产域已随 `f9c3f7e` 提交
分支与最终 commit：本卡后续提交为发布生产端边界
认领状态：review（原交付已审出问题并修复，待修复复核；整包 ALN-06 未关闭）

验收续记：本次独立验收仍发现CA-06-01（GC后旧ID/version可登记不同正文），需修改后复验。详见[Claude认领验收](../验收/2026-10-22-Claude认领验收.md)；前轮已修问题不重复记为未修复。

交叉审阅续记：生产 API 注册/写鉴权、正文摘要、传递 pin 与回滚/发布/运行引用的 GC 保护已在 `d77b241` 修复。下文首轮记录保留历史范围；当前状态以[交叉审阅记录](ALN-06-08-X-交叉审阅与基线准备.md)为准。

## 触发场景与行为变化

- 输入/触发：现有 Expert / WorkflowDef / ModelProfile / CapabilityEntry 清单需要统一身份；资产登记、按精确版本读取、依赖解析、生命周期迁移、Pack 装载协议验证。
- 修改前：
  - 没有统一 Asset 实体；`CapabilityEntry.status` 只有 active/experimental/disabled 三值，与契约八态无语义对应；
  - 没有 namespace、content_digest、精确版本引用与不可变校验；
  - 没有拒绝“同 ID/version 覆盖内容”的入口；
  - Pack 只有概念，没有可验证的字节清单与装载协议夹具。
- 修改后：
  - 新增资产域 `evoagent/assets/`：Asset 十一字段、八态、五 namespace、`AssetRef`（`asset_id@version`）；
  - 同 ID/version 内容摘要不同 → `AssetConflict`，绝不覆盖；契约字段与摘要完全一致时幂等；
  - 消费者只能精确读取，仓储没有 latest/别名解析入口；
  - 晋升 approved/active 必须有来源引用与评测证据，依赖缺失/环/pin 不兼容一律拒绝；
  - 登记/迁移/回收写入 `asset_events` 台账；GC 只回收 retired 且无精确引用的版本；
  - Pack 用真实字节（sha256 + size）验证，`LoadedPack.installed=False` 诚实标记未安装；
  - Manifest 引用形状固定为 `asset_id + version + content_digest`，读取时三者必须一致；
  - 旧专家/工作流/模型/能力条目有确定性的只读映射，缺失信息逐条列出；
  - 发布生产端边界：`ReleaseManifest` 增加冻结 `asset_refs` 并纳入签名摘要；
    含资产引用的发布必须携带资产仓储才能通过精确版本/摘要/生命周期校验；
    下发载荷包含引用并标记需端侧验签，不宣称端侧已安装。

## 范围

- 独占新增：
  - `evoagent/assets/entity.py`（契约字段、八态迁移、精确引用）
  - `evoagent/assets/content.py`（规范化摘要、Pack 清单/字节校验）
  - `evoagent/assets/mapping.py`（现有实体映射与缺失信息策略）
  - `evoagent/assets/repository.py`（不可变仓储、依赖/生命周期/引用/GC）
  - `evoagent/assets/refs.py`（Manifest 冻结引用校验）
  - `evoagent/assets/__init__.py`
  - `evoagent/core/schema/assets.py`（`assets` / `asset_events`，域名 `assets`）
  - `evoagent/api/assets_api.py`
  - `tests/test_asset_identity.py`（53 项）
- 本线整合（生产端边界）：
  - `evoagent/release/manager.py`：`asset_refs` 字段、构建期形状校验、
    `verify_assets()` 精确读取门禁、`deploy(asset_repo=...)` 资产校验；
  - `evoagent/release/delivery.py`：对账载荷下发冻结引用，标记端侧验签责任；
  - `tests/test_release_assets.py`（8 项）。
- 复用未改：`core/contracts.py` 的 `ASSET_TYPE` / `ASSET_NAMESPACE` / `ASSET_LIFECYCLE` 作为枚举唯一权威；`core/persistence.py` 未改（schema 域自动发现）。
- 未接线（按任务卡单列合入）：
  - 生产 API 注册与管理员写鉴权已在 `d77b241` 接线，并由全新子进程 HTTP 测试验证，不再依赖测试导入。
  - `core/contracts.py`、共享持久化公开方法、controller、Task Manifest 资产引用由整合角色处理。
  - Android 端字节验签/解包由 android 线负责；本卡只提供服务端字节格式与摘要。
  - `release/manager.py`、`release/delivery.py` 的生产端边界已在 `54f8058` 实现；端侧安装不在此结论内。
- 数据迁移：无。新表由 `CREATE TABLE IF NOT EXISTS` 幂等建出；老库直接可用。

## 验证证据

- 正常路径：`tests/test_asset_identity.py`（53 项）覆盖登记/精确读取/幂等/生命周期/依赖/引用/回滚/GC/Pack 字节/映射/HTTP 端点；`tests/test_release_assets.py`（8 项）覆盖发布清单签名/部署门禁/下发载荷。
- 拒绝路径：
  - 同版本不同内容 → 409 / `AssetConflict`；初始 approved/active → 拒绝；
  - 依赖缺失、成环、pin 不兼容 → `DependencyError`；
  - 证据或来源缺失 → `LifecycleError`；
  - Pack 篡改、缺文件、多余文件、绝对路径/`..`/反斜杠 → 拒绝；
  - 发布引用缺摘要/重复版本摘要冲突 → 构建即拒绝；引用不存在、摘要不符、未晋升、缺仓储 → 部署拒绝。
- 命令与结果：
  - `python -m pytest tests/test_asset_identity.py tests/test_release_assets.py tests/test_release.py -q` → **66 passed**
  - `python -m pytest tests -q` → **776 passed, 1 skipped**（含另一会话并行修复的 E-12/E-13；仅 1 条运行租约线 teardown 线程告警）
- 验证边界：全部为本地 SQLite 与确定性字节夹具；**未**验证真实 Skill/Pack 端侧装载、未做 Android 公钥验签、未接入运行 Manifest、未做真实发布流程验收。

## 回滚与未完成事项

- 回滚：按提交范围回退代码，保留 `assets` / `asset_events` 数据与审计；已有资产、发布或运行引用时不能删表/删内容。必要时先停新消费者，再设计兼容迁移。
- 保留限制：
  - API 端点已注册；资产写操作由统一鉴权要求 admin；
  - 运行 Manifest 尚未接线到 `refs.manifest_asset_ref`；冻结运行仍使用组件版本字符串（引用形状与摘要校验已提供）；
  - Android 侧字节验签、安装/退役回执未实现；
  - 服务端 HMAC 签名与端侧公钥验签职责分离：本卡未向客户端传递任何服务端签名秘密，也未实现端侧验签；
  - 能力目录（`CapabilityEntry`）映射需要持有方补充精确版本；ALN-10 已补 capabilities() 枚举接口；
  - 生产端边界已接入发布清单，但发布通道的端侧字节验签、安装/退役回执仍未实现（android 线）；
  - “别名/最新指针”被刻意不实现；未来若引入别名，必须保证冻结运行仍读精确版本。
- 影响主题：ALN-06（整包未关闭）、F-11（Capability Asset 实体）、ALN-09/ALN-11 不因本卡关闭。

审阅结论：原交付交叉审阅已完成并发现缺陷；修复待其他审阅人复核，不自批修复。
整包结论：仍实现中（本卡首轮基础完成，不关闭 ALN-06）

## 2026-10-27 共享事务接续修复

518aa26统一建表/登记/迁移/依赖审计/GC的事务所有权，禁止提前提交宿主事务和审计失败半写；31新探针、固定14模块243通过。原接受历史保留，当前review待最终复核；已有dsh-lead二次确认118/4探针，不自批。见[复核报告](../验收/2026-10-27-资产仓储共享事务复核.md)。
