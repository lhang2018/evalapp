# ALN-24-B 交付：能力状态、权限与渐进授权

任务：ALN-24-B（PRODUCT-2026-10-25，父包 ALN-24）
负责人：zcode-main；审阅人：待定；基线：`5e2b7e3`
分支/commit：feature/user-direction `b59e586`（端点+投影）+ `d1272c9`（App 权限页）
认领状态：delivered（review，待独立复核）

## 行为变化
- GET /capabilities/status：每个能力复用 check_tool 干跑（纯策略裁决）
  与 registry.health() 三值语义投影六档：ready/permission_required/
  denied/dependency_missing/unverified/unregistered，附原因与恢复路径。
- 显式 DENY 策略与 deny 模式区分；高风险链在 confirm 模式下展示
  "待授权"（执行时会走审批/确认）。
- 客户端 projectUserCapabilities：用户视图 + 恢复路径；无障碍未授权时
  open_app 覆盖为 needs_setup；canExecute 守卫——blocked/unknown 不可执行。
- App 权限页新增"能力状态（六档）"卡（d1272c9）。

## 验证
- test_capability_status 5 项（六档投影/高风险待授权/未注册/拒绝不可
  执行/未注册投影）+ 客户端 capabilities.test 4 项；后端合并回归 11 绿。
- 客户端 272 全绿，tsc 0 错误。

## 范围与缺口
- 数据只读自注册表/权限权威，无第二权威；真实设备/宿主资源压力实测
  按 ALN-25-B 另验（本卡不覆盖）。
- server.py 仅新增导入行（共享文件登记范围内）。

审阅结论：待审
