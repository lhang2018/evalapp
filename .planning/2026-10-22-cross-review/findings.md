# 发现

- 当前 HEAD bd33f56；无 .codegraph；其他人在途修改仍为 Android 通知及 tests/test_e12_e13.py。
- 最新隔离候选875/1，不是831；ALN-04已在72d20f0提交。
- ALN-06-A/08-M交付待审但workboard仍in_progress；基线卡仍写23d3950和ALN-04未提交，存在过期阻塞描述。
- ALN-06交付明确列出assets_api尚未由server导入；需验证真实API服务可见性与鉴权。
- 本轮可交叉审阅claude-main的06/08；不能自审自己开发的04/10。基线准备可以继续，正式发布需记录审阅边界。

- 原实现复现测试25 failed/2 passed，日志reproduction-tests.txt；问题包括生产资产404、operator可写、正文损坏仍可读/晋升、rollback_ref不保留、传递pin漏检、非法token/价格、线程间last_usage串用、不可用模型/视觉回退和供应商错误回显秘密。
- 已补资产正文读取校验，所有递归节点pin校验，回滚引用与持久化release/runtime/task/run引用保护；API由server注册，统一鉴权限制资产写操作。
- 已补每线程last_usage及同一适配器调用采集串行，非法用量回退显式estimate、非法价格/溢出保留unknown；未知价格无成本加分，选择不可用/不支持能力模型时拒绝或尝试下一个可用候选；错误仅保存状态/类别。
- 原路由测试填写价格却未声明pricing_known，按新契约补显式已知价格，而非把unknown重新当免费。
- 新增34项专项，与资产/发布/计量/预算/路由/诊断共178 passed。后续补干净提交全量与客户端验证。
