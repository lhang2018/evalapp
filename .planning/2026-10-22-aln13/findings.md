# 设计核对

09卷Goal十态已固定在contracts；goals schema仍占位。当前允许Task引用未注册Goal，需在正式Goal域接入后改为新任务校验注册及授权。最终动作已有AuthorizationStore两次核验，可复用以动态拒绝撤销/过期/父目标暂停。

自主等级使用独立命名，不复用工具L级枚举；建议不产生任务，准备只允许只读工具，沙箱通过既有受控SandboxRunner，授权执行/长期推进仍受任务权限与预算约束。
