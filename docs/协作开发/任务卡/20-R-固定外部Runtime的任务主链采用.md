# ALN-20-R｜固定外部Runtime的任务主链采用

优先级：P2；阶段：4；建议开发线：runtime。
状态：unclaimed；owner/reviewer/branch/base_commit均以workboard实际认领为准。
来源：本轮STATUS-SPLIT-2026-10-26，关联ZG-11；[待接清单](../待接需求_2026-10-26.md)、[本轮状态与拆分](../新增状态复核与需求拆分_2026-10-26.md)。
需求：B-75 / G2 / G3。

## 目标

在已有真实Ollama PoC之上增加显式任务级采用入口，验证工具循环、取消、恢复、预算及授权的等价边界。

## 依赖与开工条件

- depends_on：ALN-20-P, ALN-18-H, ALN-15-P
- 环境：需已固定外部Runtime、本地模型和受控资源预算
- 已有切片复用；开发可用已交付待审接口做独立准备，正式接受需实际集成与依赖核对。未就绪条件不能靠Mock宣称关闭。

## 写入边界

- evoagent/runtime/adapter.py
- evoagent/runtime/external_poc.py
- evoagent/core/controller.py
- evoagent/app.py
- evoagent/config.py

共享文件精确接线按workboard.shared_files串行整合；认领前检查他人在途修改。同一App.tsx/app.py/controller.py修改不得并发覆盖，独占模块可先开发。不新建第二套任务、身份、预算或副作用权威。

## 验收

1. 按固定Runtime/模型摘要执行真实任务并记录实际采用，拒绝版本漂移或能力声明不足
2. 外部事件不能冒充本系统run/effect权威，取消/异常/重启后不重放未知副作用
3. 记录并发和长时稳定性、实际资源/费用未知项；PoC成功不自动激活生产默认
4. 给出正常、失败、拒绝/恢复路径的精确源码、命令、日志及产物；组件、集成、设备和生产证据分别记录。场景未跑保持未测，开发完成不替代独立接受或发布。

## 交付

交付文档写清触发前后行为、复用与新增、迁移/兼容、验证和保留缺口；提交后填写40位delivered_commit并转review。owner/reviewer未分配保持null，不自批；若扩大边界，先同步任务卡与分期清单。
