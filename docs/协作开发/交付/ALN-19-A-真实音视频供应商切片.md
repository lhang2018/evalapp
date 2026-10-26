# ALN-19-A 真实音视频供应商切片（契约/预算/追溯/恢复，夹具明确标注）

任务：ALN-19-A / 真实音视频供应商漫游/剪辑切片（阶段4，父包 ALN-19，需求 F02）
负责人：dsh-lead（本会话认领；用户指示重扫台账后认领——重扫后唯一 READY 卡）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`bf3a2eab1a1f3906106e60451d39b165c35b672a`（认领时 HEAD；基线 published `21899e6`）
最终 commit：`f772860963abca9445d53ef74f42935583a7e521`（tree `00b3f4d014be107f5436a20b7603b51fda9f8a52`）
认领状态：claimed → **review（供应商契约切片；真实供应商凭据/预算未提供，不冒称关闭）**

## 触发场景与行为变化

- 触发：ALN-19 需接入真实音视频成品与计划/评测/费用；当前**没有任何真实供应商调用入口**，费用/版本/授权无法追溯，失败无边界恢复。
- 修改前：`tools/` 无音视频供应商客户端；无配置/预算/调用/费用追溯与退款语义。
- 修改后（新增 [tools/media_vendor.py](evoagent/tools/media_vendor.py)）：
  1. `MediaVendorConfig`（`from_env`）：provider/base_url/api_key/model_version/预算/单价/超时；`ready` 要求真实配置齐全，`public()` 不回显密钥；
  2. `MediaVendorClient`（默认真实 urllib HTTP 传输，可注入 transport）：
     - **未配置真实供应商/预算 → 拒绝**（绝不伪造产物）；
     - `render_clip` / `edit_clip` 调用前按秒/按操作预估费用并与剩余预算比对，不足即拒绝且**不发请求**；
     - 回执校验 `content_b64`/`asset_id`，计算 sha256，按供应商 `vendor_cost_usd` 对账；记录 provider/model_version/actor/reason/cost/remaining/asset 追溯，可选写审计；
     - 失败（网络/HTTP/回执非法）记录失败并 `refunded=True`、**不扣预算**，恢复后可继续；
     - `edit_clip` 只允许 `trim/concat/subtitle`，未授权操作拒绝。

## 范围

- 独占模块：`evoagent/tools/media_vendor.py`（新增）、`tests/test_media_vendor.py`（新增）。
- 未改其他文件；无 schema/迁移；未新增第二套预算/身份权威（费用与授权上下文由调用方提供，本切片只做供应商侧契约与追溯）。
- 测试夹具：`tests/test_media_vendor.py` 内**本地 HTTP 夹具供应商**，文件头与证据均明确标注"非真实供应商/生产实测"。

## 验证证据

- 专项：`python -m pytest tests/test_media_vendor.py -q` → **6 passed**：
  - 配置缺失 → 拒绝；
  - 真实 HTTP render：asset_id/内容 sha256/费用 0.1/剩余 0.9/Authorization/供应商 model_version 全部核对，审计含 provider/model_version/asset/sha/cost；
  - 预算不足：拒绝且**夹具零请求**；
  - edit 允许操作成功计费；未授权操作拒绝；资产不存在失败**不扣费**且失败审计；
  - 供应商 500 → 失败不扣费，恢复后成功（calls=1/failures=1）；
  - env 配置解析（api_key 不回显）。
- 实测证据：`.planning/2026-10-26-aln19a/media-vendor-evidence.json`，**11 项检查全过**，含 `fixture_label` 与 `boundary` 字段说明夹具性质。
- 全量后端：`python -m pytest tests/ -q` → **2386 passed / 0 failed / 2 skipped（397.28s）**，日志 `.planning/2026-10-26-aln19a/backend-tests.txt`。
- **真实供应商边界（如实登记，不冒称通过）**：
  - 无真实供应商**凭据/预算**：未做真实生成、真实计费对账、生产网络/超时/限流实测；
  - `render_clip/edit_clip` 的供应商协议按通用 JSON 契约实现，真实供应商字段/鉴权/价格模型需按凭据与文档核对后适配；
  - 未接 App/CLI 入口（本切片为 tools 层能力）；未采真机/生产证据。

## 回滚与未完成事项

- 回滚：`git revert f772860963abca9445d53ef74f42935583a7e521`（删新增客户端/测试；无 schema 变更）。
- 保留缺口：
  1. **真实供应商集成**（凭据+预算+真实生成/计费）未做——卡片正式验收条件未满足；
  2. 真实供应商协议适配（字段/鉴权/价格/限流）待凭据与文档；
  3. 费用治理与既有 `billing/CostGovernor` 的联动（本切片自记账，未接全局预算权威）；
  4. CLI/App 入口与生产证据未做。
- 影响：ALN-19 的"供应商契约/预算校验/费用-版本-授权追溯/失败退款恢复"前置切片交付；真实关闭仍需凭据与预算。

## 审阅结论：待审（夹具切片，不替代真实供应商验收）

## 整包结论：仍实现中（真实凭据/预算、协议适配、费用权威联动、生产证据待补）
