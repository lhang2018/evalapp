# ALN-03-P 交付报告：生产信任根与元数据制品联验

- 作者：claude-main　状态：**review**（审阅人待定，不自批）
- base_commit：见台账登记（认领时固定）
- 卡片：`docs/协作开发/任务卡/03-P-生产信任根与元数据制品联验.md`
- 需求锚：G2 / G6（EA-REL-005）

---

## 一、交付物

| 文件 | 内容 |
|---|---|
| `evoagent/release/identity.py` | 生产信任根模块（约 460 行）：`PrivateIdentityKey`（RSA 签发）、`TrustedIdentity`/`IdentityRegistry`（身份生命周期）、`canonical_artifact_message`（端侧对齐规范消息）、`verify_artifact_signature`（双实现验签） |
| `tests/test_release_identity.py` | 30 项测试：规范消息 5、签发验签 5、跨实现 3、私钥边界 5、身份登记簿 10、信封集成 2 |
| `.planning/2026-10-25-aln03-p/mutation_test.py` | 变异测试脚本：5 处缺陷注入，**5/5 被捕获** |

写入边界：全部在卡片允许的 `evoagent/release/` 内；未触碰任何共享文件与在途改动。

## 二、做什么、为什么这样做

**缺口**：端侧 `android-app/src/update/trust.ts` + `rsa.ts` 已实现 RSA PKCS#1 v1.5 +
SHA-256 验签（`TrustedKeyRing.verifyArtifact`），`delivery.py` 的 `artifact_envelope`
检查要求 `artifact_signature` / `signing_key_id` 齐全才允许下发——但**服务端没有任何
RSA 签名代码**（grep 全域零命中），信封字段没有生产来源。现有 `signing.py` 是 HMAC
对称签名，密钥不能下发端侧。

**方案**：服务端补签发侧——私钥持 RSA 私钥签发制品信封签名；身份登记簿管理公钥
生命周期（登记/轮换/撤销，记录不删可追溯，撤销需写明原因，不能撤销活动身份）；
公钥分发记录与端侧 `TrustedPublicKey` 字段直接兼容（`key_id`/`n`/`e`/
`not_before`/`not_after`），导出中**绝无私钥材料**。

**与 HMAC 的关系**：互补而非替换——HMAC 保服务端完整性链，RSA 解决"端侧不持有
共享秘密也能验真"。

## 三、跨端契约逐字节对齐（本卡核心）

端侧规范消息（`trust.ts canonicalArtifactMessage`）：七行 `\n` join：
`EVOAGENT-ARTIFACT-V1 / schema / release_id / version / digest(小写,去sha256:) /
size(十进制) / key_id`。

**联验方式（不是 mock）**：测试内直接 `node --experimental-strip-types` import 真实的
`android-app/src/update/rsa.ts`，用其 `rsaSha256Verify` 验服务端签名——
`test_device_rsa_ts_verifies_server_signature`。另与 `cryptography` 库做内部双实现
交叉验证，两种实现结论不一致时返回内部错误。

## 四、开发中发现并修复的真实缺陷（值得审阅人重点看）

**奇数长度十六进制的跨端解译分歧。** 首版用 `format(e, "x")` 导出公钥指数，
65537 → `"10001"`（5 个字符，奇数）。实测三处消费者各解各的：

- Node `Buffer.from("10001","hex")` → **截断**为 `[0x10,0x00]`（丢尾字节）；
- 端侧 `toBigInt`（rsa.ts）对奇数长度走 **base64url 分支**，解出完全不同的数；
- Python `int("10001",16)` → 0x10001（唯一正确的一方）。

结果：服务端自验通过、Node JWK 通道验签 FAIL、端侧 rsa.ts 验签 FAIL（实证于
`.planning/aln03p-crosscheck.mjs` 首轮输出）。修复：`_hex_even()` 统一补齐偶数长度
（`65537 → "010001"`），并有专门测试 `test_hex_even_envelope` 锁定该契约。
**这个缺陷单靠服务端单测永远发现不了——是跨实现联验抓到的。**

## 五、变异测试抓到的测试盲区（第二次自我纠错）

首版 M2 变异（`raw_rsa_verify` 恒返回 `True`）**未被捕获**：篡改/错钥测试只断言
`ok is False`，而 `cryptography` 交叉验证兜底把恒过变异也判为 `False`（理由是
"内部不一致"）——行为差异被掩盖，与 ALN-29-B 的弱断言教训同型。修复：拒绝场景
追加断言 `error == "签名验证失败"`（真实比较失败，而非内部错误）。修复后
**5/5 变异全捕获**。

## 六、验证记录

| 项 | 结果 |
|---|---|
| 本卡测试 | `pytest tests/test_release_identity.py` → **30 passed** |
| 端侧 rsa.ts 联验 | 真实 import 端侧实现验服务端签名 → PASS（测试内固定执行） |
| Node crypto JWK 通道 | PASS（`.planning/aln03p-crosscheck.mjs`） |
| 变异测试 | 5 处缺陷注入 → **5/5 捕获** |
| 隔离全量 | 固定源码 worktree（commit 见台账）→ 见台账 evidence |

## 七、验收对照（卡片要求 → 落点）

| 卡片验收 | 落点 |
|---|---|
| 错密钥拒绝 | `test_wrong_key_rejected` |
| 篡改元数据拒绝 | `test_tampered_metadata_rejected`（release_id/version/size 三路径） |
| 篡改制品拒绝 | 摘要进规范消息 → `test_tampered_metadata_rejected` 的 size 路径 + `verify_artifact_files`（已有，03-S）继续把关制品字节 |
| 密钥轮换可追溯 | `test_rotate_keeps_old_record`：轮换不删记录 |
| 不分发签名私钥 | `test_public_record_has_no_private_material` + 导出面只含 n/e |
| 生产或明确标注测试信任材料 | 本卡全部用**测试生成密钥**（测试内动态生成，标注明确）；生产密钥注入属部署事项，见"保留缺口" |

## 八、保留缺口（下一轮）

1. **生产私钥的注入与保管**（KMS/HSM 或运维密钥流程）不在本卡范围——本卡交付的是
   代码能力与可测试的信任根机制；`PrivateIdentityKey.from_pem` 已支持从 PEM 导入。
2. **撤销分发到端侧的同步链**（registry 变更 → 端侧 `TrustedPublicKey[]` 更新）
   需要与 ALN-11-P（生产签名与端侧安装退役回执）对接。
3. **真实制品字节联验**（对真实 APK/bundle 签名并全链验签）属 G2 取证，依赖发布
   物料，本卡用测试制品。
4. 审阅人待定：按纪律不自批。

## 九、给审阅人的建议切入点

1. 先看 `_hex_even` 的 docstring 与第四节——跨端契约分歧是实测复现的，值得复核
   修复是否完整（搜一遍仓库还有没有别的 `format(..., "x")` 公钥导出点）。
2. 第五节弱断言教训与 29-B 同型，可对照 `tests/test_release_gates.py` 的修法。
3. 跨实现测试依赖本机 Node（v22）与 `--experimental-strip-types`；环境不满足时该用例
   skip 不阻塞其余验证。
