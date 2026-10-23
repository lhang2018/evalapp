# ALN-15-A 持久预算预留

用户继续指示：实施已认领的下一张15-A，在当前项目完成开发、验证和交付。

| 阶段 | 状态 |
| --- | --- |
| 核对现有预算/持久化/Goal与正式设计 | complete |
| 固定契约与先失败的并发/崩溃夹具 | complete |
| 实现持久账户、原子预留、结算退款、unknown对账 | complete |
| 专项/预算恢复回归和固定源码全量 | complete |
| 交付与更新队列 | complete |

边界：复用BudgetLedger/Database；新增域表自动发现，不重写Goal或模型计量；本卡提供明确的账本服务接口，不虚称现有模型/工具运行时已自动接入。保留历史未提交内容，不自审或发布。

错误记录：尝试读取不存在的schema/budget.py，改查schema/tools.py中实际budget_ledger；PowerShell原生命令不展开rg路径通配，改用rg目录与glob过滤。

全量首轮的两条异步测试清理警告：以6976662修正测试等待，在固定源码全量中将线程异常升级为错误复验通过。查询不存在的tasks_api.py/approval_resume.py路径已改为从实际目录与已知controller定位，未改变业务范围。

归档差异检查：pytest原始警告日志包含带空格的空行、扫描工具含末尾空行，git diff --check对这些证据报空白问题；保留日志与其hash，不为通过格式检查改写证据。源码/文档/工具单独检查，无格式错误。
