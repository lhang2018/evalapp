# ALN-15-D 交付：设备资源准入与核心任务优先

任务：ALN-15-D 设备资源准入与核心任务优先
负责人：zcode-next
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`078b9cc`（认领锚点）；分支 feature/user-direction，交付 commit 见 git log "feat(ALN-15-D)"
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：主动帮助/后台分析（opportunity/scheduled/derived 来源）
  与用户前台任务共享资源；此前无统一的压力准入——后台任务不为主
  任务让位，也无明确的让位/恢复阈值。
- 修改后：注入 `ResourceGovernor` 后——
  1. 后台来源任务在资源高压（≥ high_water）时**准入 defer**：任务
     保持 CREATED 不转状态（状态机无 CREATED→SUSPENDED，且 defer
     不该扰动核心状态），标记 `resource_deferred` 并发事件；压力
     回落后同一任务重新 execute 即恢复，成功准入时清除过期标记；
  2. 前台（user）任务豁免后台让位阈值，只有绝对上限
     （foreground_ceiling）才暂缓——主任务在明确阈值内不被后台挤占；
  3. 运行中后台任务的让位决策 `should_pause`（仅后台来源；执行经
     既有 pause 通道，决策边界 SUSPENDED + 检查点，不绕行状态机）；
     压力回落到 low_water 以下给出 resume_candidates（迟滞防抖）。
- 未注入 governor 时 controller 行为与既有完全一致（专项断言）。

## 范围

- 独占新增：`evoagent/core/resource_admission.py`（PressureSnapshot/
  ResourcePolicy 迟滞阈值/ResourceGovernor 决策器）、
  `tests/test_resource_admission.py`（9 项）。
- 小改：`evoagent/core/controller.py`（可选 `resource_governor`
  参数 + execute 启动准入 defer/清除过期标记）。
- 未动：android-app/src（客户端准入：后台同步已有电量/网络约束，
  CPU/内存采样需原生桥，属后续卡）；pause/resume 通道；15-A/15-P
  预算账本（governor 与预算解耦：准入看利用率，计费走账本）。

## 关键语义（验收对照）

1. **明确阈值**：ResourcePolicy(high_water/low_water/foreground_ceiling)
   显式配置、构造期校验（low < high ≤ ceiling）；压力 = cpu/memory/
   model_pool 利用率最大值（clamp [0,1]）。
2. **主动分析让出**：仅 BACKGROUND_ORIGINS（scheduled/derived/
   opportunity）受 defer/pause；前台豁免有专项断言。
3. **停止不损坏核心状态**：defer 保持 CREATED 零状态扰动；pause 走
   既有通道（检查点保留）；采样失败 fail-open（只影响后台让位，
   不影响前台放行——传感器故障不杀任务）。

## 验证证据

- 专项：`python -m pytest tests/test_resource_admission.py -q` →
  **9 passed**（压力钳制/准入矩阵/仅后台暂停并登记/压力回落恢复/
  fail-open/阈值校验/E2E defer 不转状态+事件+恢复重执行/E2E 前台
  不受影响/E2E 未注入不变）。
- 相关回归：budget + budget_reservations + runtime_reservations +
  goal_governance + run_continuation → **151 passed**。
- 全量：`python -m pytest tests -q --ignore=tests/test_goal_budget_migration.py`
  → **1913 passed / 1 skipped / 0 failed**（期间一轮 28 失败经两次
  对照确认为并行提交落库与测试窗口重叠的瞬态，稳定复跑消失；
  stash 归因操作曾卷入并行在途文件，已按原样恢复并解决一处措辞
  级冲突，parallel 11-P 在途的 delivery/manager 改动未提交未触碰）。
- 边界：采样器为注入夹具（可控压力源），**真实设备/宿主的 CPU/
  内存/模型池测量与阈值标定按任务卡环境另验**；客户端原生采样桥
  未做（写入边界内，后续卡）。

## 回滚与未完成事项

- 回滚：revert 单提交；不注入 governor 零行为差异。
- 未完成（按任务卡原范围保留）：
  1. 客户端（android-app/src）资源采样与后台同步准入接线（需原生
     采样桥）；
  2. should_pause 的周期执行挂点（当前提供决策 API，自动巡检属
     周期守护 13-P 联动）；
  3. 真实设备压力阈值标定与模型池利用率采样器实现。
- 影响主题：G-08（前台/后台调度）、S-10（低资源降级）、G-09/F04/
  G4/G5；为 25-A（主动帮助）提供统一让位能力。

## 审阅结论

待审。建议重点：defer 保持 CREATED 的语义（对照 SUSPENDED 方案的
取舍：无合法转换且避免状态扰动，代价是任务重执行而非断点续跑——
后台分析任务可接受）；fail-open 方向的安全论证；迟滞阈值默认值。
整包结论：仍实现中。
