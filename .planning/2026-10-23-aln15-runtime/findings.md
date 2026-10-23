# 接线发现

- 四个实际模型入口：IntentClassifier._model_classify、PlanningEngine._model_plan、ReactEngine.run，以及工具runner的final_guard。
- 分类会吞普通异常做启发式回退，必须让预算/租约拒绝传播，不能把拒绝变成成功兜底；规划已有BudgetExceeded传播。
- 原费用计量先计次数再结算，新的聚合账户镜像不能又写一遍runtime/provider池；实验Runner事后汇总也需避免与Task实时消费重复。
- Goal budget_ref保留任务硬限额语义，聚合账户另行绑定。引用必须从DB权威Task/Goal父链/可信实验上下文取得，不能从manifest_extras取得。
- 多个独立资源上限需要一次事务占用（Task/Goal父链、日池、实验），单一父链不够，扩展15-A为多根原子分配并保留全部引用。
- 发送前要重新检查授权、动态收窄限额与unknown；免费动作需要显式已知免费零金额预留，不绕过调用量。
- 单根账户不足以表达日池+实验+Goal，扩展多根原子占用而非重复串行reserve。Task跨午夜新动作切换日池，旧预留保留日期。
- 账本unknown与物理副作用unknown必须分开：动作已成功但费用未知，不能改写动作结果或重放。账单证据可覆盖本地免费声明。
- 分类/规划早期失败没有最终验收budget证据；成本消费者须优先读取execution_budget_state，不能仅扫描result_evidence。
- 旧Goal历史消费的自动无损聚合迁移未实现，选择显式拒绝而非新增全额；旧Task快照幂等导入，宿主日池在写锁内扣除旧runtime基线。provider/expert旧池及非Task模型治理未冒充本卡完成。
