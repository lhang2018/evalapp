# ALN-10-H 验收证据加固

核对独立审阅报告后，确认 CXR-01/02/03 均为 codex-runtime 实现缺口，源码修复提交 `8218ed0`。

## 修复行为

- CXR-01：缺失资产存储返回明确错误；回归门禁也检查 unavailable/invalid 状态，防止旧持久化记录的空 errors 绕过。资产库成功读取但无关联注册仍为 not_registered，可按已有能力范围评测。资产仓库装配错误也进入不可核验结果。
- CXR-02：不再跳过空明细；所有基线/候选报告（含独立保留集两份）必须有明细、非空字符串用例 ID、唯一且数量匹配。费用可核对性、非法数值和夹具复现检查统一适用于保留集。
- CXR-03：failure_fixture 用例必须显式 `cost_known is True`，不能仅凭 cost 字段推断。匹配复现摘要不能代替费用证据；普通旧 TaskRunner 的 cost-only 契约保持兼容。合法回归测试执行器补充显式声明。

## 验证与待审

新增 `tests/test_cxr_hardening.py` 共 41 项，先在修复前复现 **26 failed / 15 passed**，修复后联合相关六份测试 **186 passed**。覆盖自定义 TaskRunner 和 ControllerTaskRunner 的 fixture_runner 两条入口，确保费用拒绝不依赖“夹具未复现”兜底。

精确源码提交 `8218ed056555d313c6809fff68bc1351ed63e560`（tree `68a8e97902b26c2be6efec6426e571b8dbedf010`）干净克隆全量 **956 passed / 1 skipped**，运行前后均干净。日志 SHA256 见 `.planning/2026-10-22-cxr-hardening/prepared-baseline.json` 与 `clean-backend-tests.txt`。客户端无提交变化，沿用同目录 tree 的既有 136 项及 typecheck 证据，本轮未重跑。

本卡为 review，尚未独立复核。claude-main 对原 ALN-10-Q/R 等卡的既有 accepted 结论保留，不表示已审阅本补丁。基线仍 candidate，未发布。CA-08-01/CA-06-01/CA-08-02、ALN-04-UI 真机和真实候选隔离执行等事项按原归属继续跟踪。
