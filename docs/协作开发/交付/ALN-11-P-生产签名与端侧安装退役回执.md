# ALN-11-P｜生产签名与端侧安装退役回执

- 卡片：[11-P-生产签名与端侧安装退役回执](../任务卡/11-P-生产签名与端侧安装退役回执.md)
- 父包：ALN-11；需求引用：G0 / G6（分期计划 PRODUCT-2026-10-25，阶段 2）
- 作者：claude-main；状态：review（待独立审阅，审阅人未定，不自批）
- 认领锚点：base_commit `078b9cc4dabbc1437361daa97a07c6a3897b0eeb`（2026-10-25 认领；实际接手锚点并非已发布共同基线，baseline 仍 candidate）
- 依赖：ALN-03-P（review，本卡按其"待审接口可用于独立实现"条款开发）、ALN-11-W（accepted）

## 目标与范围

联验生产信任、公钥验签、安装/激活/退役回执与灰度回滚。复用 03-P 的
`identity.py`（RSA 生产身份签发/撤销）、11-A 的下载/验签/装载回执与
11-W 的健康回滚；不重写已接受切片。

## 实现内容

### 服务端（evoagent/release/）

**manager.py（退役生命周期状态）**
- 新增 `retired_records` 与 `retire(release_id, *, reason, actor)`：
  退役必须写明原因与操作者；记录永不删除（可追溯）。
- `verify()` 对已退役版本直接返回 `{"ok": False, "retired": True}`；
  `deploy()` 拒绝部署退役版本；`rollback()` 拒绝回滚到退役版本；
  `is_retired()` / `retired_release_ids()` 查询；重启时从
  `release_events(kind="retire")` 恢复。退役状态是发布后生命周期记录，
  **不进入清单元数据签名字段**，历史签名不受影响。

**delivery.py（退役回执 + 服务端权威信任绑定）**
- `ACTIONS` 增加 `retired`；退役回执语义：退役当前版本时清空
  current/target 指针并保留 `last_known_good_release_id`（回滚旧能力
  仍可走）；仅退役待更新目标时只清 target；无目标可解析时拒绝。
- 新增 `identity_status(key_id)` 与 `set_identity_registry()`：
  `verified`/`activation_pending`/`activated` 三类动作按**服务端清单**
  解析 `artifact_signing_key_id` 并核对 IdentityRegistry——未登记、
  已撤销、过期一律拒绝（409）；客户端自报密钥与清单不一致同样拒绝。
- 回执记录绑定 `signing_key_id` + `signing_identity_status` +
  `artifact_digest`（均取服务端清单值，不采信客户端自报）。
- `reconcile()` 增加两项检查：`release_not_retired`（退役版本不作为
  更新目标下发）与 `signing_identity_trusted`（签名身份状态可见）；
  target 增加 `signing_identity_status` 字段供端侧展示。
- 边界约定：未接入身份登记簿时记为 `no_registry` 并显式下发，不静默
  放行也不误伤既有部署——制品字节的强制验签关卡在端侧
  TrustedKeyRing（03-P 架构），登记簿接入后撤销/过期即阻止。

**signing.py**：未改动（HMAC 契约保持不变；认领时声明的"生产身份验签
辅助"最终由 identity.py 既有能力承担，无需新增）。

### 端侧（android-app/src/install/，全新独占模块）

- `journal.ts`：安装台账。条目绑定 device_id + release_id + version +
  artifact_digest + signing_key_id；落地即 `server_confirmed=false`，
  仅回执上传成功后才确认。**核心不变量——断联不冒充已安装**：
  `status()` 区分 `installed_confirmed` / `installed_unconfirmed` /
  `retired` / `retired_unconfirmed`，未确认回执期间 UI 不得显示已安装。
  台账独立存储键 `install_journal_v1`，不改动 update/manager.ts 的
  已接受状态结构；台账损坏按空台账恢复，不抛异常。
- `report.ts`：`reportInstall`（记录→上报→确认）与
  `flushInstallReceipts`（断联恢复后补传，同一 apply_id 服务端幂等吸收）。
- `retire.ts`：`retireRelease`。退役必须写明原因；同一版本+制品已有
  未确认退役条目时返回该条目（重放而非重复上报）；上传失败返回
  `ok=false` 且条目保持未确认。
- `index.ts`：统一导出。

**update/types.ts（一行加法）**：`ApplyResultRequest.action` 联合类型
增加 `"retired"`（端侧上报退役回执的类型来源，单一事实源）。

## 验收对照（卡片口径）

| 卡片验收 | 落点 | 证据 |
|---|---|---|
| 回执绑定真实设备/制品 | journal 条目五字段绑定；服务端回执记录 signing_key_id/identity_status/artifact_digest（服务端权威值） | `test_retired_receipt_clears_current_and_keeps_known_good`、`test_activation_binds_usable_identity`、`test_退役成功：回执绑定设备/版本/制品并确认` |
| 坏包拒绝 | reconcile `signing_identity_trusted` + `release_not_retired`；record_apply 拒绝撤销/未登记/过期身份与退役版本激活 | `test_revoked/unregistered/expired_identity_blocks_activation`、`test_retired_release_refused_as_gray_activation_target`、跨端联验篡改一字节即 FAIL |
| 回滚旧能力实测 | 灰度链：部署→对账→校验→激活→健康失败→回滚→当前版本恢复；退役保留 last_known_good | `test_gray_deploy_rollback_chain` |
| 断联不冒充已安装 | journal 未确认条目 → `installed_unconfirmed`/`retired_unconfirmed`；补传后转确认 | `test_断联未确认不得冒充已安装`、`test_退役上传失败：未确认，不冒充已退役` |

## 测试与证据

- 服务端新增 `tests/test_release_install_retire.py`：**20 passed**
  （退役生命周期 7、退役回执 4、信任绑定 7、灰度回滚链 2、跨端联验 1——
  含真实端侧 `trust.ts` 验服务端生产身份签发的 envelope，篡改一字节即拒）。
- 端侧新增 `android-app/test/install/`：**16 passed**（journal 10、
  retire/report 6）。
- **隔离全量（干净 HEAD + 本卡改动，worktree 隔离）**：
  后端 **1957 passed / 1 skipped / 0 failed**，客户端 **340 passed /
  0 failed**（日志 `.planning/2026-10-25-aln11p/iso-full-server.log`、
  `iso-full-client.log`）。
- 类型检查：`npx tsc --noEmit` 0 错误。
- 变异测试（`.planning/2026-10-25-aln11p/mutation_test.py`，结果
  `mutation-results.log`）：**11/11 全部捕获**（服务端 7：ACTIONS 去
  retired、退役不清空、身份核对恒放行、自报密钥不拒绝、退役可激活、
  verify 不查退役、对账不查退役；端侧 4：未确认冒充已安装、退役不要
  原因、重复退役不去重、confirm 不写确认）。双基线先证明全绿。
- 工作区全量观察：主工作区跑全量时 `test_peer_action_binding_review`
  单文件失败，重跑仍失败；worktree 隔离验证（干净 HEAD）该文件
  19/19 过——失败来自并行会话未提交的动作绑定在途改动
  （authorization.py/controller.py/binding.py 等），非本卡；本卡改动
  仅限 evoagent/release/ 与新增端侧模块。客户端主工作区全量曾出现
  4 个失败并伴随 node --test 挂起，隔离环境同命令 340/340 过——
  同样归于并行在途状态。

## 保留缺口（未验证不记通过）

1. **app.py 接线未做**：`ReleaseDelivery` 已支持
   `identity_registry` 注入，但 `evoagent/app.py`（共享集成文件，非本卡
   写入边界）尚未传入 `IdentityRegistry`。接入前服务端对带信封发布记录
   `no_registry`（显式下发、不静默）；接入后撤销/过期身份即阻止。
   需要的改动一行：装配期构造 registry 并
   `self.delivery.set_identity_registry(registry)`。
2. **真机未测**：安装/激活/退役回执的真机投递、灰度回滚的真机表现按
   卡片口径属真机矩阵范围，本轮仅本地自动化证据。
3. **App.tsx 接线未做**：`recordActivation`/`retireRelease` 的 UI 调用
   点待 App.tsx 在途改动落地后由集成步骤接入（本卡不碰 App.tsx）。
4. **package.json 测试脚本未扩**：`test:offline` 尚未包含
   `test/install/`，需脚本属主更新（本卡以显式路径跑过）。

## 共享文件修改点（按认领声明执行）

- `evoagent/release/manager.py`：退役记录/查询/拒绝路径（如上）。
- `evoagent/release/delivery.py`：ACTIONS、退役分支、信任绑定、
  reconcile 两项检查、target 一字段。
- `evoagent/release/signing.py`：未改动。
- `android-app/src/update/types.ts`：action 联合类型一行加法。
- `android-app/src/install/`、`android-app/test/install/`：全新文件。
- 未触碰：App.tsx、evoagent/api/server.py、evoagent/app.py 及并行会话
  在途文件（认领与提交前均以 git status 核对）。
