# 检查记录

f8f32f7 为 ALN-07-C 一审文档提交，169 passed 和 6 项绑定探针为历史证据，本轮尚未重跑。关注实际恢复异常清理、绑定归属和缺失证据处理；发现需真实路径验证后定性。

确认三类问题：只读工具 tool_hint 被当作副作用声明（旧 e2e 测试删除 hint 避开）；门禁在活动登记清理保护之外；计划/绑定异常直接 RUNNING/SUSPENDED→FAILED 不合法。初始新9项7失败/2通过，追加4项状态机负向全失败，首轮聚焦77通过/1状态机失败；修复后聚焦86通过。

7de95f7 修复：只复制计划投影并清除已知 none 合约 hint，未知合约保守保留、不删账本行；整个 pre-run 门禁保护活动登记；错误经 RECOVERING→FAILED，不造新 run/检查点，不开 RUNNING→FAILED 直通。SUSPENDED 增加合法 RECOVERING 边。新13项探针含3真实恢复/受控续跑、混合工具、未知工具、矛盾回执、账本故障、4类合法失败、OSError/KeyboardInterrupt 事件失败后可重试。

测试构造修正：新追加断言的检查点改通过 controller.cp_mgr 读取，并在自建快照明确 checkpoint_id，未产生虚假的业务失败数字。第一次固定提交回归误写不存在 test_host_deployment_review.py，未运行任何测试；保留 committed-validation.json/committed-tests.txt，改用已核实文件名后 final-validation.json 为最终证据。
