# ALN-29-A｜首发验收骨架与断言

## 目标与结果

将 EA-UX-008 的 17 条原始用例落实为带来源摘要、执行步骤、可执行事实断言的验收矩阵。默认无执行记录，17 项全部为 not_run。开发完成进入 review，不表示这些用户功能已经验收通过。

实现文件：

- [首发验收矩阵](../验收/首发验收矩阵.json)：逐项保留原编号、场景和通过条件，初始状态固定 not_run。
- [断言与证据核对](../../../tools/product_acceptance.py)：核对 17 项覆盖、原文 LF 摘要、断言完整性、观察值类型、精确源码版本、执行日志/事件哈希；拒绝路径逃逸、缺字段、重复用例和夹具环境。
- [命令入口](../../../tools/check_product_acceptance.py)：默认校验结构；有失败记录返回非零，--require-pass 额外要求 17 项全部通过。
- [任务台账检查器](../../../tools/check_collaboration_plan.py)：保留原23包契约保护，允许有来源和关闭标准的新增包；检查46张新增卡、28UX/17用例映射、任务依赖环和本会话单卡队列。

## 使用

~~~powershell
python -X utf8 tools/check_product_acceptance.py
python -X utf8 tools/check_product_acceptance.py --evidence path/to/records.json --source-commit <40位实际测试提交>
python -X utf8 tools/check_product_acceptance.py --evidence path/to/records.json --source-commit <40位实际测试提交> --require-pass
~~~

没有执行记录时，默认结构校验返回0、counts.not_run=17、all_passed=false；加 --require-pass 返回1。不能把默认命令的0退出码当作用户功能已经通过。

记录格式为 schema_version=1、runs 数组。每条记录包含 case_id、source_commit、environment（kind/run_id/description）、observations 和 artifacts。环境 kind 只接受 integration/device/production；fixture 不能通过。artifact 使用仓库相对路径、原始字节 SHA-256 和 kind，至少一个 execution_log 或 event_trace。

observations 为嵌套事实对象，字段和比较关系逐项列在矩阵 assertions 中。例如 UX-F01-01 要求 answer.present=true、execution.unnecessary_complex_tasks=0。布尔值不能用1代替；运行Manifest必须是非空标识；重试上限必须为正数。

## 验证

本卡工具与台账保护专项 **65 passed**。探针覆盖17项逐项缺少观察值、丢失/减弱用例、伪造初始通过、错误版本、被篡改日志、路径逃逸、夹具、重复记录、类型混淆、资源数字异常、缺失映射、依赖环及当前队列多卡开工。

运行命令：

~~~powershell
python -X utf8 -m pytest tools/tests/test_product_acceptance.py tools/tests/test_product_backlog.py -q
python -X utf8 tools/check_collaboration_plan.py --verify-current
python -X utf8 tools/check_collaboration_plan.py --verify-prepared
~~~

源码提交、命令日志与文件摘要由 [committed-evidence.json](../../../.planning/2026-10-25-product-breakdown/committed-evidence.json) 固定。原23包设计映射、声明与行号检查另有对应日志。本次没有重跑业务全量测试。

## 边界与后续

- 该工具检查提交的观察值及日志完整性，不能独立证明观察来自真实运行。日志存在且哈希一致，不等于现场行为真实；采集器接线、真实任务执行、真机结果与独立评估仍需实施。
- 17个产品用例当前全部未执行。单元测试中的合成记录只验证断言引擎，不入产品验收记录。
- 首发 G0—G7 门禁由29-B单独维护，不复制第二套发布批准权威。本卡没有修改业务运行时、APK或发布状态。
- 新包及原包缺口详见[分期实施](../需求拆分与分期实施_2026-10-25.md)。下一张已认领23-A，按现有Task权威统一自然语言任务契约；后续在实际开工时更新其接手锚点。
