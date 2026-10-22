# ALN-11-A 端侧真实制品下载、验签、装载与回执

## 行为变化

`UpdateManager.prepare()` 不再只凭服务端声明的 `artifact_digest` 放行，而是要求目标同时给出 `artifact_url`、`artifact_size`、`artifact_sha256`、`artifact_signature`、`signing_key_id` 与签名 schema；缺任一项直接拒绝，既不下载也不进入激活路径。

下载走独立的 `ArtifactDownloader`：先写 `stagingDir/<release>.part`，按 `Range` 分块续传（已有 `.part` 从现有偏移继续，检查点 KV 记录 url/摘要/总量/etag），每块写入前检查剩余空间，总量受 `maxBytes`（默认 64 MiB）约束；中途短读、超长、空间不足或摘要不符都会中止并保留/清理暂存文件，只有完整字节通过 SHA-256 后才原子改名为正式制品。已存在且摘要匹配的正式制品直接复用。

验签在真实字节落盘后进行：`TrustedKeyRing.verifyDigest()` 将 canonical envelope（`EVOAGENT-ARTIFACT-V1` + schema + release/version + 小写摘要 + 大小 + key id）交给内置 RSA PKCS#1 v1.5 / SHA-256 校验，未知、已撤销、未生效、已过期密钥一律拒绝，摘要或大小与声明不符同样拒绝。验签失败时制品不落正式路径，状态转为 `failed` 并记录原因。

激活前依次检查：目标最低 App 版本（按三段版本比较）、`required_tool_contracts`、本机已授予权限、验证阶段是否完成、安全时机窗口（`activationAllowed`）；随后先调用注入的 `BundleApplier.apply()` 装载到非活动槽，装载成功才切槽并清除待用制品，失败则保持原活动槽、上报 `activation_failed`。重启后若停在 `verified` 且未注入装载器，`activate()` 拒绝切槽，不会出现“只改标记未装内容”的假激活。

回执新增 `version` / `artifact_digest` / `signing_key_id` 字段，绑定实际装载的制品而非对账目标；发送前先把 `PendingReceipt`（含固定 `apply_id`）持久化，成功后移除，失败保留；`flushReceipts()` 以同一 `apply_id` 幂等重放。`healthCheck` 与 `rollback` 回执同样携带实际版本与摘要。

## 接口与兼容

- 新增 `android-app/src/update/artifact.ts`：`ArtifactSource`、`ArtifactFileSystem`、`ArtifactDownloader`、`ArtifactDownloadError`。
- 真机适配：`rangeSource.ts`（XHR `arraybuffer` + `Range`/`If-Range` + `Content-Range` 解析，服务端忽略 Range 时按偏移裁剪）、`nativeArtifact.ts`（`NativeArtifactFiles` 桥接原生命令）、`base64.ts`（无 Buffer 的 base64/UTF-8 编解码）、`workflowBundle.ts`（双槽写入 `update_bundle_v1:<slot>` 与 `update_bundle_active_v1`，校验 JSON 对象且含 `workflow` 字符串）、`devTrust.ts`（内置开发公钥 JWK）。
- 原生桥：`UpdateModule.kt` 提供 `stat/append/read/rename/remove/freeBytes`，路径限定在应用私有 `filesDir` 内；`UpdatePackage.kt` 注册到 `MainApplication`（未改动并行会话的 `DevicePackage.kt`）。
- 服务端：`ReleaseManifest` 新增 `artifact_url/artifact_sha256/artifact_size/artifact_signature/artifact_signing_key_id/signature_schema`（不并入元数据 HMAC 签名字段，避免历史发布验签失败）；`delivery.reconcile` 在声明 `artifact_url` 时校验 envelope 完整性并在目标中下发；`GET /artifacts/<release_id>` 支持 206/416/full 三种响应；`tools/seed_update_release.py` 用本机私钥生成真实 envelope 并落盘制品（私钥不入库/日志/提交，客户端只内置公钥）。
- 新增 `android-app/src/update/trust.ts`：`TrustedPublicKey`、`ArtifactEnvelope`、`canonicalArtifactMessage`、`TrustedKeyRing`。
- 新增 `android-app/src/update/rsa.ts`：JWK `n/e` 公钥、PKCS#1 v1.5 SHA-256 验签、hex/base64url 签名解码。
- `types.ts`：`UpdateTarget` 增量可选制品/权限/工具契约字段；`UpdateState` 新增 `artifact`、`receipts`；`UpdateSlot.signing_key_id`；`ApplyResultRequest.version/artifact_digest/signing_key_id`。
- `manager.ts`：新增 `BundleApplier`/`BundleApplyRequest` 与 `UpdateManagerOptions.artifactSource/artifactFiles/downloader/trustedKeys/bundleApplier/grantedPermissions/activationAllowed/stagingDir/maxArtifactBytes`；新增 `flushReceipts()`。
- 旧持久化状态无需迁移：`parseState` 对缺失的 `artifact`/`receipts` 补空值，旧状态可直接读取。
- 既有 `verify()` 路径保留（仅校验声明摘要 + 空间），用于尚未接入真实制品的调用方；真实下载路径必须走 `prepare()`，未注入下载器或受信公钥时直接失败，不会降级为只信声明。
- 端侧不接管 APK 自身升级，`client_binary` 仍由平台商店机制处理。

## 验证

- 新增 `android-app/test/update/artifact.test.ts` **10 passed**：断点续传与原子改名、坏摘要/截断不落正式文件、空间不足拒绝、真实 RSA 字节验签与篡改/未知/撤销/过期密钥、prepare→activate 全链路及回执字段、缺权限/低版本/坏签名拦截、低空间不触达激活、装载失败保持活动槽并上报 `activation_failed`、重启未装载不得切槽、失败回执持久化后按同一 `apply_id` 重放。日志 `.planning/2026-10-22-aln-11-16/aln11-targeted.txt`。
- 既有 `test/update/manager.test.ts` **19 passed**，无回归。
- 客户端全量 `npm run test:offline` **154 passed**；`npm run typecheck` 无错误。日志 `.planning/2026-10-22-aln-11-16/client-full.txt`、`typecheck.txt`。
- 服务端全量 `python -m pytest -q` **1076 passed / 1 skipped**，日志 `.planning/2026-10-22-aln-11-16/python-full.txt`。
- 新增 `android-app/test/update/nativeArtifact.test.ts` **4 passed**、`tests/test_update_artifact_delivery.py` **5 passed**：base64/UTF-8 往返、槽位与 active 标记、非法 bundle 拒绝、Range 206/200 裁剪、envelope 完整性拦截、`/artifacts` 206/416/404、新增字段不破坏元数据签名。客户端全量复跑 **158 passed**。
- **真机联调（Xiaomi 24018RPACC / HyperOS）**：服务端以 `.evoagent-update` 启动，`seed_update_release.py` 生成 `rel_dev_v2`（v9.1.0，bundle 205 B，sha256 `c60b8ada…`，key `dev-release-1`）并灰度 100% 部署；debug APK `adb install -r` 成功。设备 UI 依次：检查更新 → `available`；下载并验签 → `verified`（205 B，摘要 `c60b8ada…`）；应用私有目录 `files/updates/rel_dev_v2.bundle` 大小 205、`sha256sum` 与服务端一致；激活候选版本 → `active · 活动槽位 B`，AsyncStorage 写入 `update_bundle_v1:b` 与 `update_bundle_active_v1`（`demo/v2`）；健康检查通过 → `已知良好 v9.1.0`。服务端 `device_installations.inst_mvgemk5601b3r1lded2g` 状态 `healthy`，`device_deployments` 依次记录 `verified → activation_pending → activated → health_check`（均 `ok`）。杀进程重启后仍显示 `当前能力包：demo/v2 · v9.1.0 · 槽 b`。取证：`.planning/2026-10-22-aln-11-16/ui5.xml`（available）、`ui6.xml`（verified）、`ui8.xml`（active）、`ui9.xml`（healthy）、`ui11.xml`（重启后）、`screen-active.png`、`RKStorage.db`。
- 源码提交：`10ec664c2804b4485b8c60ac8f053c1b6b10e8ae`（客户端内核）；`26aad1b56512364d0cdb3b616eeec6a781703532`（真机适配、服务端 envelope/Range、联调工具）。

## 回滚与未完成事项

1. 回滚触发：真机下载/装载路径出现不可恢复失败。回滚操作：恢复 `android-app/src/update/` 与 `android-app/test/update/` 至上一提交；持久化状态多出的 `artifact`/`receipts` 为可选字段，旧代码读取时忽略，无需数据迁移。
2. 真机联调只覆盖 205 B 单块完整下载（服务端返回 206）；中途断连续传、弱网、断电与磁盘写满是 Node 测试覆盖，未在真机上做故障注入。
3. 回滚路径未在真机触发：本次已知良好版本与候选同为 `v9.1.0`，仅在单测中覆盖“装载失败保持活动槽 / 自动回滚”。
4. 内置的是开发公钥（`dev-release-1`），生产公钥分发、轮换与吊销流程仍待接入；私钥仅存在于本机 `.planning` 下且被 `.gitignore` 排除。
5. 未做多设备灰度与系统级健康探测；健康判定仍由调用方以 `healthCheck(ok)` 上报。
6. 未认领/未改动并行会话文件与共享台账（`DevicePackage.kt`、通知模块、`backgroundRunner.ts`、`test_e12_e13.py` 等保持原状）。

审阅结论：待审（owner `claude-main`，reviewer `codex-runtime`，不得自审自收）。
