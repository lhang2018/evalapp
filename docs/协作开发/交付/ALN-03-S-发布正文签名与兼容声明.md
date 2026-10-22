# ALN-03-S 发布正文签名与完整兼容声明

## 行为变化

发布清单不再信任调用方传入的布尔门禁。`ReleaseManager` 现在按规范化字节重建签名载荷，用实际制品文件字节重算逐文件摘要，再验证清单签名与制品清单；正文、文件清单或声明被改写都会使验证失败。

- 签名载荷覆盖清单身份（release_id/version/channel/created_at）、制品文件清单与逐文件 `{path,size,sha256}`、发布者密钥标识与时间；`manifest_digest` 由同一规范化字节计算。
- 制品验证只认 `artifact_dir/<release_id>/` 下的真实文件：缺文件、多余未声明文件、大小/摘要不符、越界路径与符号链接均拒绝。
- 稳定通道必须声明目标平台、最低 App 版本、权限、工具契约、数据 Schema、模型能力、离线能力与回滚支持；缺项时错误信息指明具体字段。draft/internal 显式放宽并在验证结果标记 `compatibility_relaxed`。
- 每次发布校验的成功/失败都写入 release event（context/ok/key_id/payload_digest/artifact_digest/errors），可按 release_id 追溯。
- `KeyRing` 支持新增、切换与移除密钥；旧清单在密钥保留期间持续可验，移除对应密钥后即拒绝。
- 旧格式 `hmac-sha256-v1` 不再默认放行：可验证的旧清单需显式 `migrate_signature` 重新签名；不可验证的旧清单必须同时提供 `allow_unverified=True` 与非空 actor，迁移后 `legacy_verified=False` 入账。

## 契约与兼容

- 新模块 `evoagent/release/signing.py`：`evoagent.release.signing/1` 载荷、`hmac-sha256` 算法、`hmac-sha256-v2` 清单版本与自定界规范化编码。
- `ReleaseManifest` 新增 `artifact_files`、`rollback_supported`、`signing_key_id`、`signing_payload_digest`、`created_at`、`signature_version` 等字段；默认服务端密钥标识为 `server-hmac-1`。
- 制品目录布局为 `artifact_dir/<release_id>/...`，签名信封为 `artifact_dir/<release_id>.json`。
- 服务端只做 HMAC 与文件字节校验；端侧公钥信任、下载装载与回执仍由 ALN-11/Android 线负责，本次不下发服务端秘密。

## 验证

- 新增专项 `tests/test_release_signing.py` **22 passed**：规范化字节与非法值、载荷身份/渠道/时间/文件绑定、真实文件篡改、缺失/多余/未声明文件、清单自改写、构建期缺文件与越界路径、未知密钥、错误签名、密钥轮换、稳定通道参数化缺项、草稿放宽、未知通道、成功/失败证据持久化、旧格式迁移与不可验证旧格式。
- 发布与记忆联合专项 **151 passed**，日志 `.planning/2026-10-22-aln-03-05/targeted.txt`。
- 全量 `python -m pytest -q`：首轮 **1051 passed / 1 skipped**，其中 2 项为共享台账校验失败（`IMPLEMENTATION.md` 仍称 `ContextManifest` 全仓不存在，且 line-anchored 引用因本次源码行号位移指向空行），另 1 项异步审批用例偶发 `SUSPENDED`；复跑 **1055 passed / 1 skipped**，审批用例通过，仅余同样 2 项台账失败（并行会话新增用例使总数上升）。共享台账属其他会话独占，本卡未修改。日志 `.planning/2026-10-22-aln-03-05/full.txt`、`full-rerun.txt`。
- 源码提交：`53fc7bb940168b59364888634a296ecf39f4c051`。

## 回滚与未完成事项

1. 回滚触发：发现合法发布被误拒或密钥轮换不可用。回滚操作：恢复 `evoagent/release/` 至上一提交并重跑发布专项；旧清单在新代码下仍拒绝，不会静默降级。
2. 端侧验签、制品下载装载与真实设备回执未关闭，依赖 ALN-11/Android 线。
3. 当前为服务端对称 HMAC 校验；非对称公钥信任、密钥托管与生产密钥轮换流程需独立设计。
4. 共享台账需由对应会话更新：`IMPLEMENTATION.md` 的 M-07 状态与行号引用、`FEATURES.md`/`GAP_ANALYSIS.md` 的裸引用随源码位移失效。

审阅结论：待审（owner `claude-main`，reviewer `codex-runtime`，不得自审自收）。
