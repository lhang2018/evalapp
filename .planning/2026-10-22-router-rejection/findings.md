# 发现

- HEAD61d3b94，无.codegraph；其他人在途通知、tests/test_e12_e13.py与.workbuddy不纳入。
- _route_model在规划和ReAct阶段各调用一次；当前except Exception回退react.model并记录source=router。
- execute外层已有FAILED/诊断/task_failed与run收尾，因此路由拒绝应向外抛出，不需要新增任务状态或第二套收尾。
- 失败时必须清除上一阶段已成功的model_route，记录router_rejected或router_error和error_class，不冒充成功选择。
- 正常路由及unknown override告警后走常规路由的行为保留。ALN-08验证段应明确标首轮历史并链接909/1的最终干净候选证据。

- 原实现新增6项回归中5 failed/1 passed，日志reproduction-tests.txt；其中执行阶段拒绝后确实调用suggest_action，证明不只是字段归因问题。
- 修复为记录无选中模型的router_rejected/router_error及model_route_failed事件，再原样抛给execute现有收尾。允许路由器自己授权的可用fallback，不允许控制器绕过策略。
- 修复后运行/迁移/清单/资产模型审阅/路由联合100 passed；包含真实不可用默认模型、unknown override、普通异常、规划后拒绝和合法fallback。
