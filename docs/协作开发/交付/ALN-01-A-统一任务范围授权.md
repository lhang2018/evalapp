# ALN-01-A 统一任务范围授权

负责人codex-runtime。用户要求完成ALN-01/02；本卡在既有允许列表和异步审批之上补齐任务范围，不改变原工作包依赖。

## 行为与接线

- App运行前为任务冻结独立authorization_ref，记录服务器授权人、工具/权限集合、有效期；解析出来的required_tools/required_permissions不充当授权。旧任务首次执行补齐，已有引用丢失则拒绝重发。
- 网关同时检查持久任务授权、当前专家工具集合、调用方允许列表、系统风险策略和预算。替代工具、DAG、ReAct与补偿共用此入口；空列表不视为无限制。
- 支持path_roots、targets、device_ids、max_risk。路径按工具resource_base和实际resolve结果核验，目录前缀相似或路径穿越不能扩大范围。ToolContract.resource_bindings可声明自定义嵌套资源字段，resource_defaults声明实际执行默认值；内置文件工具已接入。
- 回调获得输入副本，执行使用冻结参数。同步/异步确认均不能覆盖系统deny。最终进入runner之前再次检查撤销、到期、授权修订、工具版本和当前专家范围；任务终止后不发新动作。
- 新授权修订和撤销会吊销该任务未使用的审批。审批绑定包含授权修订、工具版本、参数、工作流节点及风险/权限；旧审批不能用于新动作或新授权。已消费审批不重新激活。
- 工具结果和任务运行清单带authorization_ref，执行证据可对应实际授权修订。

## 使用接口

任务提交可传authorization_scope，作用是约束任务，不能扩大专家或系统允许范围：

```json
{"tools":["file_read"],"permissions":[],"path_roots":["E:/project"],"targets":[],"device_ids":[],"max_risk":"low"}
```

省略某类资源字段表示该类未额外收紧；显式空数组表示不允许该类资源。权限集合始终明确列出。自定义工具必须按实际访问对象声明resource_bindings，不能把请求需求字段当权限证明。

管理员端点：

- GET /tasks/{task_id}/authorization：读取当前服务器授权。
- POST /tasks/{task_id}/authorization：body为policy与ttl_seconds，创建不可变修订，旧审批失效。
- POST /authorizations/{ref}/revoke：撤销并传播到后续动作与待消费审批。

端点均在生产ApiServer注册并执行admin检查；授权修订独立落库，不覆盖正在变化的任务状态。Task.authorization_ref保留运行快照引用，查询最新修订使用授权端点。

## 验收与状态

新增test_aln01_authorization覆盖允许/拒绝、对象/设备/路径、权限和风险、空集合、回调期间撤销/过期/修订/版本变化、参数冻结、真实App与生产HTTP鉴权。ALN-01/02联合专项日志及精确干净提交全量见 `.planning/2026-10-22-aln01-02/`。

本卡代码交付后进入review，待独立验收；不自批、不宣布正式基线发布。ALN-01传递影响19个后续包，但ALN-03/06可沿原依赖并行推进。后续包可对接本卡接口，整包状态仍以独立验收为准。

## 精确提交与验证证据

最终源码提交 `22c50cabd3c432ca5f891efb633652a669fcb4a6`，tree `5c3d7765f191abdf81e92136008a854970eb9812`；业务实现由71f91ea及后续修复提交组成。全部18项源码/测试的Git blob和SHA256清单见 `.planning/2026-10-22-aln01-02/source-files.json`。

干净克隆全量 **1065 passed / 1 skipped（113.30s）**；新增49项独立专项通过；并发/排序3个用例在哈希种子0、1、42各通过。运行前后工作树均干净；唯一skip为历史台账负向样本不适用，非业务或硬件路径。客户端tree未变，沿用既有136/typecheck，未声称本轮重跑。

中间遗漏提交和并发失败日志保留为历史证据，不混作最终结果。台账/设计映射/引用漂移与协作检查通过后交独立review，本卡未自批。
