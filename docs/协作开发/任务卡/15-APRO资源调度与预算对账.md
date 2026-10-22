# ALN-15-A：预算预留/结算/退款与未知费用对账契约

建议角色：integration（`evoagent/core/budget.py` 为集成共享文件）；调度与设备资源桥后续拆 runtime/Android 卡。依赖 ALN-02-A（回执对账，review）、ALN-08（计量，未关闭）、ALN-13-A（Goal 预算引用，在途）：本卡先定契约与夹具，不替上游做验收，也不改 Goal 域。

## 目标

现状：`evoagent/core/budget.py` 已有 `BudgetLedger`/`BudgetController` 的用量记录、模型费用结算与 unknown 标记，但没有「调用前预留 → 成功结算/失败退款 → 崩溃回收」的共同契约，也没有与 Goal/Task/实验预算引用的绑定规则。本卡交付预留—结算—退款—对账契约与夹具，先证明并发任务可同时花掉同一额度、崩溃预留不可回收等缺口。

## 写入边界

可独占新增：`evoagent/core/reservations.py`（预留/结算/退款/回收，复用 `BudgetLedger` 持久化）、`tests/test_budget_reservations.py`、本卡交付文档。

可在本卡内小改：`evoagent/core/budget.py`（只加预留入口与查询，不改既有计费/限额语义）。

不得触碰：`evoagent/goals/`（ALN-13-A 在途独占）、`evoagent/models/` 计量实现（ALN-08 在途）；`evoagent/api/server.py`、`evoagent/app.py`、`evoagent/core/controller.py` 的接线另交小卡，不在本卡直接改。设备资源桥（CPU/内存/磁盘/电量）属后续 Android/runtime 卡。

## 验收

- 同一额度上的并发预留不得双重占用；超限预留被拒绝且原因可查。
- 预留记录在调用前持久化；成功按真实用量结算，失败按策略退款，unknown 进入对账而非清零。
- 进程崩溃后遗留预留可被识别并回收/对账，不重复计费。
- Goal/Task/实验的预算引用可追溯到同一预留账本；子目标不能扩大父额度。
- 夹具先证明现状缺口，实现后同一夹具转绿。

## 交付

契约、夹具、实现、命令与日志，使用[统一交付模板](../交付模板.md)。调度分级（前台/同步/评测/进化）与低电量/低空间降级另卡；本卡不宣布 ALN-15 整包完成。
