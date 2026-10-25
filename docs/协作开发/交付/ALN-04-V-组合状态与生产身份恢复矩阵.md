# ALN-04-V 交付：组合状态与生产身份恢复矩阵

任务：ALN-04-V 组合状态与生产身份恢复矩阵
负责人：zcode-next
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`a038f21`（认领锚点）；分支 feature/user-direction，交付 commit 见 git log "feat(ALN-04-V)"
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：任务授权此前**没有消费身份维度**——`action_authorizations`
  只记录 actor（发放人），`check()` 不校验请求方身份，任何 principal
  都能消费同一任务授权（跨身份复用畅通）；组合等待状态（问答+审批
  同时在途）的恢复保留未矩阵化验证。
- 修改后：
  1. **授权绑定提交身份**：`issue(principal=...)` 把 principal 写入
     policy 保留键；`check(principal=...)` 比对——绑定授权对其他身份
     与匿名请求**一律拒绝**（fail-closed）；旧记录（未绑定）保持兼容
     放行。principal 贯穿全链：submit 存 manifest → ReAct/工作流
     authorization_context → registry 两处 check。
  2. **组合状态恢复矩阵**（测试固化）：崩溃接管把 planned 副作用转
     unknown 保留（不吞）+ 07-C 预绑定意图同窗口保留；问答+审批组合
     等待跨重启（同库新实例）完整保留；重启后授权身份绑定仍生效。

## 范围

- 独占新增：`tests/test_recovery_matrix.py`（10 项）。
- 修改：
  - `evoagent/core/authorization.py`：issue/check 增加 principal 参数
    与比对（normalize 白名单 + 保留键，无表迁移）；
  - `evoagent/tools/registry.py`：两处 check 从 authorization_context
    透传 principal；
  - `evoagent/react/engine.py`：run 增加 principal 参数 →
    authorization_context；
  - `evoagent/workflows/engine.py`：run 增加 principal →
    execution_context → 节点 authorization（工作流路径同样受身份约束）；
  - `evoagent/core/controller.py`：submit 把 principal_id 写入运行
    清单（local:host 默认值不写）；react.run / workflows.run /
    授权 issue 三处传入。
- 未动：`evoagent/api/server.py`（提交方 authorization_scope 的身份
  绑定接线留待该文件在途改动合入后另做，本卡记为缺口——scope 授权
  默认不绑定 principal，legacy 兼容语义不受影响）。

## 验证证据

- 专项：`python -m pytest tests/test_recovery_matrix.py -q` →
  **10 passed**（同身份放行/跨身份拒/匿名拒/legacy 兼容/身份切换重
  授权/非法 principal 拒；崩溃接管 planned→unknown 保留+意图保留/
  组合等待跨重启保留/重启后身份绑定保持/submit 记录 principal）。
- 相关回归：test_aln01_authorization + async_approvals + approvals +
  execution_boundaries + auth_rejection_transport + workflow +
  binding_atomicity + run_continuation + aln02_receipts →
  **159 passed**。
- 全量：`python -m pytest tests -q --ignore=tests/test_goal_budget_migration.py`
  → **1827 passed / 1 skipped / 1 failed**；唯一失败
  `test_ledger_drift`（GAP_ANALYSIS.md:177 裸引用 `cli.py:232` 指向
  空行）经 stash 对照核实为 integration 流文档引用漂移，与本卡无关
  （上轮的 runtime_reservations 失败已被并行会话修复，本轮消失）。
  排除文件为并行 15-P 在途半成品（IndentationError）。
- 边界：身份切换/重启均为本地模拟（同库新 App 实例、租约过期接管）；
  **真实部署身份**（多真实用户/token 轮换/生产 auth 中间件联测）与
  多进程 kill 注入按任务卡"真实部署身份环境"另验。

## 回滚与未完成事项

- 回滚：revert 单提交；principal 为 policy 保留键，无迁移逆转；
  未绑定 principal 的既有授权行为完全不变。
- 未完成（按任务卡原范围保留）：
  1. server.py 提交方 `authorization_scope` 的 principal 接线
     （该文件并行在途，串行合入后补）；
  2. 组合等待的用户裁决 UI 联动（04-UI/23-E 范围）；
  3. 真实多身份部署联测与生产 auth 中间件对接。
- 影响主题：R-09（Action Guard 授权绑定身份）、S-01（最小权限——
  授权不跨主体）、O-10/G3（多设备/身份一致性）、F03（恢复）。

## 审阅结论

待审。建议重点：匿名拒绝对系统内部调用路径的覆盖面（sandbox/coding
等无 principal 上下文的 execute 是否存在带绑定授权又必须匿名执行的
场景——本卡全量回归未发现，但建议复核确认）；principal 存 policy_json
保留键而非独立列的取舍。
整包结论：仍实现中。
