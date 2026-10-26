# 记忆策略运行时约束与待接汇总

用户继续开发并要求收尾后把识别到的文档与缺口放入待接文档。起点3229728；沿用planning-with-files，不触碰Claude的app.py/cli.py/integration/制品测试在途内容。

| 阶段 | 状态 |
|---|---|
| 检索、范围策略与主链核对 | complete |
| 复现与修复统一检索约束 | complete |
| 固定源码相关验证和清理 | complete |
| 待接文档汇入、台账登记和提交 | complete |

范围：memory/store.py、user_control.py、新策略读取模块与专项测试；无设备、真实模型、外部服务。使用合成记忆，新临时目录自动回收，旧缓存不删除。默认无策略保持现有行为，已存策略沿用allow/deny语义，不新增绕过参数，不扩大身份/Grant。
