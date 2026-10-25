# ALN-29-E 稳定版本、更新控制与回滚体验

任务：ALN-29-E / 稳定版本、更新控制与回滚体验
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`bae3914bc8b8b26e836cbc3190cb5df930859dc6`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `e16dbfeedd48d3e58cc24d68886eb76f2d7770d7`（tree `b7884d403f29b76d90251def09f45fb94d80dd57`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：用户要「看得懂稳定版/实验版、能控制要不要更新、出问题能看到真实回滚结果」。
- 修改前：
  - `ReleaseManager` 已有真实签名/摘要校验、部署、回滚、退役（ALN-11-P/29-B 等已接受）；`delivery.py` 有灰度分桶与回滚目标解析。但**没有用户视角的通道选择与更新决定层**：稳定/实验版本的区分、实验版显式开启、固定版本、坏包/降级/退役的可解释拒绝、回滚证据与目标版本绑定，都散落在底层或未成文。
- 修改后（新增 `evoagent/release/channels.py`，**未改** manager/delivery/signing/identity）：
  - `UpdatePreference`：安装级 `channel=stable|experimental`、`auto_update`、`allow_experimental`、`pinned_release_id`；可选 JSON 持久化（不新增数据库迁移）。
  - `explain()`：用户可读说明——当前通道/当前版本、稳定与实验版本列表、自动更新状态、固定版本、最近一次回滚（目标版本、是否校验通过、原因）。
  - `evaluate_update()`：可解释决定（install / rollback / already_active / refuse）：
    - 稳定通道安装**拒收**实验版本；实验版本需显式 `allow_experimental`；
    - 已退役版本拒绝；固定版本时拒绝切换其他版本；
    - **禁止无约束降级**（仅 `as_rollback=True` 走回滚入口）；
    - 坏包（签名/摘要/制品校验失败）拒绝且错误逐条可见；
    - 任何切换都返回 `activation.requires_restart=True` + `must_rebind_release_identity=True` + 说明文案（GLOBAL-01：运行绑定不可无约束热切换），且**不改变当前运行版本**。
  - `rollback_with_evidence()`：调真实 `ReleaseManager.rollback`，返回目标版本/摘要/签名校验结果、`same_version_binding`（回滚证据与目标版本绑定同一版本）、不可逆动作警告；`history()` 可查历史决定。

## 范围

- 独占模块：`evoagent/release/channels.py`（新增）。
- 共享文件补丁：**无**（未改 `manager.py`/`delivery.py`/`signing.py`/`identity.py`/`server.py`/客户端）。
- 新增/修改契约：新增通道偏好、更新决定、回滚证据三类数据结构与规则；无 schema 变更、无迁移。
- 兼容：纯新增模块，既有发布/回滚/退役行为不变。

## 验证证据

- 正向：
  - 稳定通道默认；实验通道需显式开启（未开启抛 `ChannelError`）；
  - 实验通道安装实验版本 → `install` 决定 + `requires_restart=True`，且当前运行版本不变（无热切换）；
  - 回滚：真实 `rollback` 到 `v1.0.0`，`verification.ok=true`、`same_version_binding=true`、`signature_ok=true`，`explain().latest_rollback.verified=true`。
- 失败/拒绝：
  - 稳定安装请求实验版本 → 拒绝（原因可见）；
  - 坏包（签名被篡改）→ 拒绝，`verification.errors` 非空；
  - 降级 → 拒绝，`as_rollback=True` 才允许；
  - 已退役版本 → 拒绝；固定版本 → 拒绝切换到其他版本；
  - 无部署记录时回滚 → `ok=false` 且原因可见。
- 命令、结果、日志与对应源码 commit：
  - 提交 `e16dbfeedd48d3e58cc24d68886eb76f2d7770d7`；
  - 本卡专项：`python -m pytest tests/test_release_channels.py -q` → **12 passed**；
  - 发布域回归（release/assets/channels/evidence/gates/identity/install_retire/signing）→ **168 passed**；
  - 证据生成：`.planning/2026-10-25-aln29e/release-evidence.json`，`all_checks_pass=true`（8 项检查全过，含真实 HMAC 签名摘要、坏包拒绝、回滚证据绑定）；
  - 全量后端：`python -m pytest tests/ -q` → **2053 passed / 0 failed / 1 skipped（414.01s）**，日志 `.planning/2026-10-25-aln29e/backend-tests.txt`。
- Mock / 夹具 / 真实边界：
  - 签名使用 `ReleaseManager` 的真实 HMAC 签名/校验路径（测试密钥，**不是生产 RSA 身份密钥环**）；
  - 无真机更新/回滚复验：`android-app/App.tsx`/`src/update/*` **未改**，客户端「稳定/实验切换 + 更新按钮 + 回滚结果展示」需后续客户端接线（本卡只交付服务端通道/决定/回滚证据层）；
  - 生产信任材料（真实公钥/私钥、发布制品包）未参与本轮验证。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert e16dbfeedd48d3e58cc24d68886eb76f2d7770d7`（删新增模块与测试；无 schema/迁移）。回滚后既有发布/回滚行为不变。
- 保留的限制、后续 ALN 和依赖：
  - 客户端 UI（稳定/实验版本展示、更新控制、回滚结果页）未接；
  - 生产身份密钥环与真实发布制品包未参与（使用 HMAC 测试密钥）；
  - 未接 API 端点（`api/` 未改）；
  - 灰度百分比与自动更新的组合策略未在真机/多安装实例上验证；
  - 实验通道默认关闭，需要用户显式开启的设计已实现但无 UI。
- 影响的主题/实体/ADR/契约/系统验收条目：新增通道/更新决定/回滚证据契约；既有发布签名与部署契约不变；UX-28/GLOBAL-01 的服务端部分交付。

## 并发交付补充（2026-10-25 复核）

本卡在并行会话中同时产出两层实现，已在同一 HEAD 下组合复核：

| 层 | 提交 | 内容 | 验证 |
|---|---|---|---|
| 服务端（本报告） | `e16dbfeedd48d3e58cc24d68886eb76f2d7770d7` | `evoagent/release/channels.py`：通道偏好/更新决定/回滚证据 | 12 passed；发布域 168 passed；证据 all_checks_pass |
| 客户端（zcode-next，`commit 8828fdc`） | `8828fdc`（含 `android-app/src/update/control.ts` + `App.tsx`） | 版本角色可理解、GLOBAL-01 运行中任务禁止热切换、拒绝原因可见、回滚证据 from→to+digest | `node --test test/update/control.test.ts` → **5 passed** |

两层互补：服务端做通道/决定/回滚证据，客户端做用户可见展示与热切换守卫；本卡按**组合交付**进入 review，审阅应同时覆盖两个提交。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-29：客户端体验、生产信任材料与真机复验仍待；本卡交付通道、更新控制与回滚证据层）
