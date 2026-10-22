# ALN-11-A 端侧真实制品下载、验签、装载与回执

## 行为变化

`UpdateManager.prepare()` 不再只凭服务端声明的 `artifact_digest` 放行，而是要求目标同时给出 `artifact_url`、`artifact_size`、`artifact_sha256`、`artifact_signature`、`signing_key_id` 与签名 schema；缺任一项直接拒绝，既不下载也不进入激活路径。

下载走独立的 `ArtifactDownloader`：先写 `stagingDir/<release>.part`，按 `Range` 分块续传（已有 `.part` 从现有偏移继续，检查点 KV 记录 url/摘要/总量/etag），每块写入前检查剩余空间，总量受 `maxBytes`（默认 64 MiB）约束；中途短读、超长、空间不足或摘要不符都会中止并保留/清理暂存文件，只有完整字节通过 SHA-256 后才原子改名为正式制品。已存在且摘要匹配的正式制品直接复用。

验签在真实字节落盘后进行：`TrustedKeyRing.verifyDigest()` 将 canonical envelope（`EVOAGENT-ARTIFACT-V1` + schema + release/version + 小写摘要 + 大小 + key id）交给内置 RSA PKCS#1 v1.5 / SHA-256 校验，未知、已撤销、未生效、已过期密钥一律拒绝，摘要或大小与声明不符同样拒绝。验签失败时制品不落正式路径，状态转为 `failed` 并记录原因。

激活前依次检查：目标最低 App 版本（按三段版本比较）、`required_tool_contracts`、本机已授予权限、验证阶段是否完成、安全时机窗口（`activationAllowed`）；随后先调用注入的 `BundleApplier.apply()` 装载到非活动槽，装载成功才切槽并清除待用制品，失败则保持原活动槽、上报 `activation_failed`。重启后若停在 `verified` 且未注入装载器，`activate()` 拒绝切槽，不会出现“只改标记未装内容”的假激活。

回执新增 `version` / `artifact_digest` / `signing_key_id` 字段，绑定实际装载的制品而非对账目标；发送前先把 `PendingReceipt`（含固定 `apply_id`）持久化，成功后移除，失败保留；`flushReceipts()` 以同一 `apply_id` 幂等重放。`healthCheck` 与 `rollback` 回执同样携带实际版本与摘要。

## 接口与兼容

- 新增 `android-app/src/update/artifact.ts`：`ArtifactSource`、`ArtifactFileSystem`、`ArtifactDownloader`、`ArtifactDownloadError`。
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

## 回滚与未完成事项

1. 回滚触发：真机下载/装载路径出现不可恢复失败。回滚操作：恢复 `android-app/src/update/` 与 `android-app/test/update/` 至上一提交；持久化状态多出的 `artifact`/`receipts` 为可选字段，旧代码读取时忽略，无需数据迁移。
2. **真机适配尚未接线**：`App.tsx` 仍只传 `appVersion`，RN 侧文件系统（分块 append/rename/freeBytes）、HTTP Range、系统时钟安全窗口与 bundle 装载器适配器未实现；当前交付是端口完备、可注入、Node 测试覆盖的客户端内核，真机默认不会自动下载制品。这是 ALN-11 剩余主要工作。
3. 服务端尚未下发带 `artifact_url/artifact_signature` 的发布 envelope，联调需 `release/delivery.py` 侧补齐签名输出；本卡未改该文件。
4. 未做多设备灰度、真实弱网/断电注入与系统级健康探测；健康判定仍由调用方以 `healthCheck(ok)` 上报。
5. 未认领/未改动并行会话文件与共享台账。

审阅结论：待审（owner `claude-main`，reviewer `codex-runtime`，不得自审自收）。
