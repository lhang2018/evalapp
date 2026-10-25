# ALN-29-E 交付：稳定版本、更新控制与回滚体验

任务：ALN-29-E 稳定版本、更新控制与回滚体验
负责人：zcode-next
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`e16dbfe`（认领锚点）；分支 feature/user-direction，交付 commit 见 git log "feat(ALN-29-E)"
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：用户需要理解当前/候选版本角色、控制更新时机、看到
  真实回滚结果与坏包拒绝原因。
- 修改前：更新面板只有 phase 状态与按钮；激活不检查运行中任务
  （GLOBAL-01 运行绑定可被无约束热切换）；拒绝原因只有一行内部
  错误；回滚结果没有版本配对证据展示。
- 修改后（新增 `android-app/src/update/control.ts` + App 接线）：
  1. **版本角色可理解**：已激活槽 = 稳定轨（11-P 语义）；候选角色由
     `target.channel` 判定（experimental/canary/beta/dev → 实验轨）。
  2. **GLOBAL-01 守卫**：`canActivateNow(state, runningTasks)`——
     存在运行中任务（SUBMITTED/WAITING_DEPENDENCY）时激活被拒并给
     用户可读理由（ADR-012 运行绑定不可热切换）；守卫在 UI 决策层
     （activateUpdate 前置 Alert），manager 状态机与既有
     activationAllowed 钩子不变。
  3. **拒绝可见**：failed.last_error 与 blocked.reasons 映射为用户
     可读通知（制品验签失败/缺少工具契约等不再是黑盒）。
  4. **回滚证据绑定同一版本**：rolled_back 回执投影为
     from→to 版本配对 + digest + 时间（`rollbackEvidence`），
     面板展示。

## 范围

- 独占新增：`android-app/src/update/control.ts`（纯投影/守卫，
  不改更新管理器状态机）、`android-app/test/update/control.test.ts`
  （5 项）。
- 接线（写入边界内）：`App.tsx`——activateUpdate 前置守卫 +
  更新面板版本角色/最近拒绝/回滚证据三行展示。
- 未动：`evoagent/release/`（服务端发布语义不变；GLOBAL-01 的
  服务端侧强制属 24-B 能力状态/04 控制面既有语义，本卡是端侧
  决策层）；manager.ts（守卫消费既有状态，不改状态机）。

## 验证证据

- 专项：`node --test test/update/control.test.ts` → **5 passed**
  （角色投影/GLOBAL-01 守卫（有任务拒+无任务过）/无候选拒绝/
  拒绝可见两形态/回滚证据配对与缺失）。
- 全量客户端：`npm run test:offline` → **342 passed / 0 failed**；
  `npm run typecheck` 零错误。
- 边界：全部为本地自动化（内存 UpdateState 夹具）；"生产信任材料
  与可连接设备"环境下的真实激活/回滚体验（真实签名包切换、渠道
  实验轨下发、真机回滚可视化）按任务卡环境**另验**，本卡不含。

## 回滚与未完成事项

- 回滚：revert 单提交（纯新增 + App.tsx 局部接线）。
- 未完成（按任务卡原范围保留）：
  1. 生产信任材料（03-P 范围）下的真实激活/回滚联测；
  2. 服务端侧的 GLOBAL-01 强制（运行中任务的目标下发拦截，属
     delivery/24-B 联动，本卡是端侧决策层）；
  3. 实验轨（channel=experimental）的真实下发与灰度体验。
- 影响主题：UX-28、GLOBAL-01、ADR-012（运行绑定不可热切换——
  端侧决策层落地）、O-07（版本分发/回滚体验）。

## 审阅结论

待审。建议重点：运行中任务的判定口径（SUBMITTED/WAITING_DEPENDENCY
计入、WAITING_USER 不计——等待用户输入的任务不占执行资源）；
守卫在 UI 层而非 manager 层的取舍（manager 无任务可见性）。
整包结论：仍实现中。
