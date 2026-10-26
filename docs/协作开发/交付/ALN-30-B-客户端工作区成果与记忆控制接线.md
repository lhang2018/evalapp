# ALN-30-B 客户端工作区成果与记忆控制接线

任务：ALN-30-B / 客户端工作区成果与记忆控制接线（P0，阶段1，Android 线）
负责人：dsh-lead（本会话认领；按用户"按优先级顺序认领"取 P0）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`2f112276754ae10567e10d62bcfe21b11863037d`（认领时 HEAD；基线已 published `21899e6`）
最终 commit：`1b590a38b5900a3670e45387870e1441dc5c7a0e`（tree `3f6ff1964aee2bdda769db1bd0eb30cb2332e984`）
认领状态：claimed → **review**（仅本地组件切片，设备验收未做）

## 触发场景与行为变化

- 触发：用户在 App 里需要看到「真实任务的成果文件/预览、任务→节点→产物的追溯」，并在「记忆」页做来源查看、纠错、删除与范围启用/禁用。
- 修改前：`src/features/{artifacts,workspace,projects}` 已有纯视图模块但 **未接入 App.tsx**；记忆页调用旧接口（`/memory`、`/memory/{id}/correct`、DELETE），不展示来源/保留边界，也没有范围禁用；服务端 `24-A`（任务产物）与 `24-D`（记忆控制）已验收但客户端未消费。
- 修改后：
  1. `api.ts`：新增 `taskArtifacts / artifactPreview`（24-A）与 `memoryControlItems / correctMemoryItem / deleteMemoryItem / memoryScopePolicy / updateMemoryScopePolicy`（24-D），全部支持 adminToken；保留既有方法不破坏旧调用。
  2. `src/features/artifacts/panel.ts`（纯函数）：真实文件才可打开；预览截断/二进制/空结果/加载失败分别给出诚实提示；每张卡带「节点 + 产物」追溯；未关联服务端任务明确提示无成果可查。
  3. `src/features/memory/control.ts`（纯函数）：记忆行展示来源/范围/状态/关联任务；删除结果展示**保留边界（墓碑/审计/备份）与失败项**、不宣称全域擦除；范围禁用/恢复做合法性校验；离线/401/403 有明确提示。
  4. `App.tsx`：任务卡新增「工作区成果」面板（查看/刷新/打开预览；无真实文件显示"不提供打开/下载入口"）；记忆页改用控制端点，新增「分析范围（禁用后模型不再收到该范围内容）」开关与删除保留边界展示，纠正改为控制端点并沿用真实 API。

## 范围

- 独占模块：`src/features/artifacts/panel.ts`、`src/features/memory/control.ts`（新增）；`test/offline/{artifactPanel,memoryControl}.test.ts`（新增）。
- 共享文件补丁（精确修改点）：`android-app/src/api.ts`（追加接口与类型）、`android-app/App.tsx`（成果面板 + 记忆页改造）。
- 未改：`src/features/{workspace,projects}` 既有模块（尚未接线）、后端任何文件。
- 兼容：纯新增接口/视图；旧 `/memory` 方法保留；无迁移、无 schema 变更。

## 验证证据

- 客户端专项：`node --test test/offline/artifactPanel.test.ts test/offline/memoryControl.test.ts` → **13 passed**（真实文件判定/截断/二进制/空结果不冒充交付、来源与保留边界、范围禁用校验、离线与权限错误）。
- 客户端全量：`npm run test:offline` → **437 passed / 0 failed**；`npx tsc --noEmit` → **0 错误**。
- **后端契约探针**（真实 HTTP + 真实制品文件）：`.planning/2026-10-26-aln30b/api-contract-probe.json`，**8 项全过**：
  - `GET /tasks/{id}/artifacts` 返回真实制品 1 个；`GET .../preview` 返回真实文本、`truncated=false`；
  - `GET /memory/items` 返回记忆控制项且 `provenance.source` 可见；范围策略可读、admin 可更新（deny 生效）；operator 请求 `/memory/*` → **403**（沿用既有 admin 门禁）。
- 未跑的验证：真机 UI 未跑（设备验收需明确空闲窗口）；项目切换/工作区节点视图未接线；服务端无全量下载端点（只有 4KB 预览）。

## 回滚与未完成事项

- 回滚：`git revert 1b590a38b5900a3670e45387870e1441dc5c7a0e`（删新增视图模块/测试，回退 api.ts 与 App.tsx 两处追加；无 schema/迁移）。
- 保留缺口（按任务卡验收逐项）：
  1. 成果：列表/预览/追溯已接；**全量下载未实现**（服务端只有 4KB 预览端点，需新增下载端点或分块接口后另接）；
  2. 记忆：来源/纠错/删除/范围禁用已接真实 API 并展示保留边界；**禁用后"后续模型不再收到该范围内容"仅由服务端策略保证**（客户端只调用策略端点，未在客户端侧二次验证模型上下文）；
  3. 项目切换/工作区节点视图（`features/workspace`、`features/projects`）**未接线**；
  4. 设备/真机证据**未采**（未连接设备不称真机通过）；离线/超时/取消/权限拒绝：纯函数层已覆盖提示，UI 级联调未跑；
  5. 未新增第二套任务/身份/预算/副作用权威（遵守卡片约束）。
- 后续：需设备空闲窗口补真机证据；项目/工作区接线与全量下载端点可作为后续切片或 30-B 追加。

## 审阅结论：待审（本地组件切片；设备证据与 3 项缺口需复核判断是否拆分）

## 整包结论：仍实现中（ALN-30 客户端接线未完成：项目/工作区视图、全量下载、真机证据待补）
