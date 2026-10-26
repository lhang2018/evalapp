# ALN-16-Q｜聚合观测与受控导出删除API接线

优先级：P0；阶段：2；建议开发线：integration。
状态：unclaimed；owner/reviewer/branch/base_commit均以workboard实际认领为准。
来源：本轮STATUS-SPLIT-2026-10-26，关联ZG-10；[待接清单](../待接需求_2026-10-26.md)、[本轮状态与拆分](../新增状态复核与需求拆分_2026-10-26.md)。
需求：UX-17 / UX-24 / G2。

## 目标

为既有聚合证据查询、授权导出下载及删除执行器提供受鉴权的API消费者；复用审计、保留项和受信身份，不重做仓储。

## 依赖与开工条件

- depends_on：ALN-16-P, ALN-16-X
- 环境：可先做本地组件与拒绝路径；正式集成须核对depends_on的实际交付，未验收不冒称通过
- 已有切片复用；开发可用已交付待审接口做独立准备，正式接受需实际集成与依赖核对。未就绪条件不能靠Mock宣称关闭。

## 写入边界

- evoagent/api/evidence_api.py
- evoagent/api/routes.py
- evoagent/observability/repository.py
- evoagent/observability/export.py

共享文件精确接线按workboard.shared_files串行整合；认领前检查他人在途修改。同一App.tsx/app.py/controller.py修改不得并发覆盖，独占模块可先开发。不新建第二套任务、身份、预算或副作用权威。

## 验收

1. 真实HTTP查询任务/运行证据，跨主体及越权导出删除拒绝，不能从body伪造principal
2. 下载字节与审计/摘要匹配；删除逐层返回实际计数、残留和失败，不宣称跨备份或全域擦除
3. 聚合观测与既有feedback入口分开审计，证明端点已注册并执行服务，异常不返回假成功
4. 给出正常、失败、拒绝/恢复路径的精确源码、命令、日志及产物；组件、集成、设备和生产证据分别记录。场景未跑保持未测，开发完成不替代独立接受或发布。

## 交付

交付文档写清触发前后行为、复用与新增、迁移/兼容、验证和保留缺口；提交后填写40位delivered_commit并转review。owner/reviewer未分配保持null，不自批；若扩大边界，先同步任务卡与分期清单。
