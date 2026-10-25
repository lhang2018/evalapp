# ALN-06-R 交付：精确能力版本的运行时消费

任务：ALN-06-R 精确能力版本的运行时消费
负责人：zcode-next（新身份，2026-10-25 认领；承接原 zcode-main 开发流）
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`bf706e8`（认领时锚点；分支已随项目切换为 feature/user-direction，最终交付 commit 见 git log "feat(ALN-06-R)"）
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：执行步骤（ReAct/工作流）在冻结执行清单时，需要绑定其
  实际消费的专家/工作流的精确资产版本——不仅存储和声明版本。
- 修改前：ALN-06-A 的不可变资产仓储只被发布链（ReleaseManifest
  asset_refs）消费；controller 执行路径按 ID 查工作流库/专家注册表，
  执行清单里的 expert_ref/workflow_ref 只有版本号，没有内容摘要，
  库内容与登记资产是否一致无从校验。
- 修改后：注入 `RuntimeAssetBinder` 后，执行步在 `_freeze_execution_manifest`
  时把专家/工作流（含依赖闭包）绑定为 `asset_id@version + content_digest`，
  写入 `runtime_manifest.asset_bindings` 并发 `asset_bindings_frozen` 事件；
  退役资产不可被新运行绑定；已绑定运行在恢复/续跑时只验证存在与摘要、
  不重新绑定（旧 run 版本固定）；工作流库/专家注册表内容与绑定资产
  摘要不一致（库漂移）在执行前与续跑路径都被拒绝。

## 范围

- 独占新增：`evoagent/assets/consumption.py`（绑定协议，不执行任何
  动作、不复制工具/账本权威）、`tests/test_asset_consumption.py`（15 项）。
- 小改：
  - `evoagent/assets/repository.py`：新增只读 `versions_of()`（同 ID
    全版本查询，供确定性选择）；
  - `evoagent/core/controller.py`：可选 `asset_binder` 构造参数（不传
    = 行为与既有完全一致，专项测试断言）；`_freeze_execution_manifest`
    内绑定/校验；`_resume_suspended` 前置 pinned 校验；execute 增加
    `AssetError` 干净失败分支（FAILED + 诊断 + 事件，无副作用）；
    `created_from_task` 赋值移到 freeze 之后（运行态字段不进资产
    内容摘要，属顺序修正，不改变行为）。
- 未动：app.py 装配不注入 binder（生产装配属 ALN-06 整包/后续卡）；
  server.py 与 check_collaboration_plan.py 的其他会话在途改动未触碰；
  工具版本继续走既有 `tool_versions` 清单。

## 关键语义（验收对照）

1. **实际引用精确版本与 digest**：绑定记录含 `ref/version/content_digest/
   selected_from`；多可消费版本时确定性取最高版本（数值段比较，可复现）；
   `prefer_ref` 支持调用方持有精确决策。
2. **退役不可新绑定**：retired/deprecated/draft/candidate 之外的
   不可消费状态在 `bind()` 即抛 `AssetNotConsumable`，发生在任何节点
   执行之前（测试断言无 node_runs）。
3. **旧 run 版本固定**：`verify_pinned` 刻意不检查生命周期——资产事后
   退役不得阻断已绑定它的运行；只有绑定资产被 GC（不存在）或内容
   摘要与绑定时不一致才拒绝。
4. **库漂移拒绝**：执行对象（wf_lookup/注册表返回）内容摘要 ≠ 绑定
   资产摘要 = 库被改而资产未重新登记，执行与续跑均拒绝。
5. **依赖闭包**：绑定含全部 dependency_refs 递归解析，任一依赖缺失/
  不可消费即整体拒绝。

## 验证证据

- 专项：`python -m pytest tests/test_asset_consumption.py -q` →
  **15 passed**（绑定精确性/闭包/退役拒绑/candidate 拒绑/未登记/
  prefer_ref/多版本确定性/pinned 退役豁免/摘要漂移/GC 拒绝/E2E
  SUCCEEDED 冻结+事件/E2E retired 副作用前拒绝/E2E 库漂移拒绝/
  续跑校验不重绑/未注入行为不变）。
- 相关回归：test_node_run_id + test_asset_identity + test_release_assets
  + test_manifest_contract + test_workflow_extraction +
  test_plan_runtime_review → **115 passed**。
- 全量：`python -m pytest tests -q` → **1699 passed / 1 skipped /
  0 failed**（此前一轮出现的 2 个 goal_opportunities 失败经 stash
  对照确认为并行会话 29-A 在途瞬态，与本品无关，且复跑已消失）。
- 边界：全部为本地自动化（内存/临时目录 SQLite + Mock 资产晋升）；
  生产装配（App 注入 binder + 真实专家/工作流资产化流程）与
  App/专家真实消费链未做，按 ALN-06 整包范围另验。

## 回滚与未完成事项

- 回滚：纯新增 + 可选参数 + 顺序修正，revert 单提交即可；不注入
  binder 时零行为差异（专项测试覆盖）。
- 未完成（按任务卡原范围保留）：App 装配注入与内置专家/工作流的
  资产化登记流程；Android 端能力包资产与本地 bundle 的 digest 联动；
  退役资产对"已发布能力包仍可执行"的端侧对齐（ALN-11-P 范围）。
- 影响主题：G6（能力资产真实消费）、C-08（生命周期）、ADR-012
  （运行绑定不可变清单——asset_bindings 属运行清单冻结面）。

## 审阅结论

待审。建议重点：库漂移判定的内容规范化（created_from_task 排除的
合理性）、verify_pinned 刻意豁免生命周期状态的安全论证、
versions_of 最高版本选择规则。
整包结论：仍实现中。
