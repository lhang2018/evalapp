# ALN-05-I：记忆身份与清单运行时接线

任务：ALN-05-I / 记忆身份与ContextManifest运行时接线  
负责人：codex-runtime；审阅人：待独立分配  
实际接手基点：04de43f（当前main源码锚点，非发布基线）  
源码提交：5847f3f，后续边界修复05a7cf4  
认领状态：review；整包ALN-05未关闭，05-M原审阅与本轮作者修复不自批。

## 行为变化与范围

此前Controller只按专家memory_scope_key检索和写入，五类身份接口停在库层，ContextManifest生成后被Controller丢弃。现在Task保存不可变服务端绑定引用与基身份快照，实际ReAct装配和结果沉淀使用相同身份，保留领域蜂巢隔离，并按task/run保存来源清单及摘要。写入审计引用实际持久条目ID，重复内容不会返回不存在的ID。

新增memory/binding.py、schema/memory_context.py、api/memory_context_api.py；共享接线覆盖Task、Database、Controller、App、Config、ApiAuth及任务API/Goal提交入口。MemoryStore最小补齐已有身份接口的owner链、重复内容和事实冲突分区；不另做记忆检索引擎、执行授权网关、Goal授权或预算权威。

## 身份与API契约

- ApiAuth对已确认的静态凭证类别给出服务主体：无令牌本地管理模式为local:host，operator凭证为api:operator，admin凭证为api:admin。令牌明文不写入身份、Task、Manifest或事件。
- 一个共享operator令牌就是一个服务主体，不等于多个人分别登录。此卡没有新增用户登录、项目成员系统或完整多租户IAM；其余全局运维接口仍按既有角色边界管理，不以本卡宣称整个API已经完成多租户隔离。
- 宿主配置memory_bindings列出每个服务主体可使用的user/project/session三元组。省略memory_context采用该主体第一条配置；提供部分选择时必须唯一命中白名单。未知/歧义身份拒绝，配置中移除原三元组后旧绑定执行也拒绝，不把旧任务改绑给新用户。
- task_id只能来自服务端新Task；expert_id由实际解析到的专家memory_scope_key决定（mobile_operation专家读写mobile蜂巢），不能从请求声明获得。RuntimeManifest冻结前记录绑定ref和基身份；ContextManifest记录实际专家键及五类身份。
- 请求只接收memory_context和memory_grant_ref。直接填写identity/grant/actor式对象、memory_identity、memory_binding_ref、principal_id、task/expert等身份字段不能创建权限。Task JSON自身也不是权威，省略/修改新任务的ref或快照会被持久化和执行校验拒绝。

配置示例（宿主可信配置文件，无令牌正文）：

```json
{
  "memory_bindings": {
    "local:host": [{"user_id":"alice","project_id":"p1","session_id":"s1"}],
    "api:operator": [{"user_id":"alice","project_id":"p1","session_id":"s1"}],
    "api:admin": [{"user_id":"administrator","project_id":"ops","session_id":"ops-session"}]
  }
}
```

配置项整体省略或为空字典时，为上述三个内置主体生成各自独立的默认命名空间；明确配置时不自动补其他主体。配置不含的主体拒绝。请求示例：

```json
{"goal":"alpha knowledge","intent":"general","memory_context":{"project_id":"p1","session_id":"s1"}}
```

## 跨域授权与归属分区

AccessGrant仍是05-M的值契约，本卡只增加服务端签发记录供消费。可信宿主通过MemoryBindingAuthority.issue_grant签发，带grantee主体及三元组、target完整身份、scopes、actor/reason、期限；没有新增HTTP签发入口。客户端只能引用已有grant_ref。其他主体、其他项目/会话、伪造、过期或撤销的引用都拒绝。

授权仅扩展指定scope的目标身份，不能把target的user键自动扩张成其他scope权限，也不能改变写侧owner。读写主链及恢复会核对记录；每个后续模型/工具调用还通过既有BudgetController.authority_check复核记忆授权，并保留原Goal检查。已发出的调用不能撤回，撤销阻止后续动作；历史数据删除/导出仍属16。

记忆归属按scope层级匹配：user记忆跨自身项目继承，project匹配user/project，session匹配user/project/session，task再匹配task，expert匹配user/project/session/expert而不要求旧task相同。真正不带owner的shared是公开共享层；未声明owner的历史私有蜂巢不会因键相同而自动放给新身份。重复内容和fact_key冲突只在同一owner分区内合并。

## 持久化、恢复与兼容

新增task_memory_bindings、memory_access_grants、context_manifests三张域表；表发现机制幂等创建，无既有表ALTER，无需改写迁移1-3。Task JSON新增memory_binding_ref和memory_identity，旧JSON仍可读；不会通过从JSON复制身份来授权。

绑定行是权威且不可重新绑定。新任务初始落库、后续save_task和claim_run会核对快照；重启恢复与失败重试继承原owner，重试使用新task_id。旧无绑定任务进入legacy:<原task_id>专属user/project/session，不推断旧全局私有记忆的归属。两连接并发迁移同一旧任务时可接纳已生成的同一私有legacy绑定；回填只在事务中修改memory两字段，不覆盖并发更新的业务状态/run_id。现代绑定省略字段始终拒绝。

ContextManifest以(task_id,run_id,manifest_id)保存，不改写已经冻结的RuntimeManifest。写入校验canonical digest、任务身份和真实run归属，同一清单不可覆盖；重复装配只忽略无语义的created_at差异。新接口GET /tasks/{id}/context-manifests?run_id=...返回来源引用、摘要、选择/压缩信息及完整身份，不包含目标、约束、记忆、验收或grant理由正文。压缩元数据保留约束/验收条目引用，grant理由保留摘要。

operator只能读写其主体的任务入口；列表过滤、任务事件/恢复/清单、独立runs清单和trace列表补归属检查，旧孤立run清单不作为绕行入口。客户端幂等重放还核对主体、身份选择和grant引用。admin保留全局管理权限，全局memory管理端点需要admin。使用operator令牌的现有记忆中心会收到403；管理员入口保持可用，普通用户的私有记忆自助API/端侧接线仍需单独交付。

## 验证证据

- tests/test_runtime_memory_identity.py：真实App/Controller、SQLite、内置Mock模型/工具、HTTP及实际重启续跑；没有Mock记忆库或授权权威。涵盖五类作用域、mobile键映射、相同专家键下的用户/项目/会话隔离、伪造/撤销/过期、grant不扩scope/不改写侧owner、Task字段省略拒绝、重试新task_id、两个连接及旧快照竞争、Manifest不可改写/不含正文、API及重放旁路。
- tests/test_memory_owner_partition.py：使用原有公开接口的4项独立探针，在04de43f git archive原源码上4 failed，修复后通过；复现旧owner去重/冲突/检索及Manifest约束验收正文泄漏，不把缺少新模块导致的import错误当缺陷证据。
- 早期组合回归328 passed；第一版5847f3f固定源码1456 passed / 1 skipped，另存历史日志。随后发现两连接legacy旧快照竞争的间歇拒绝及run/trace查询边界，已追加修复和确定性回归；最终源码及结果以committed-evidence.json为准。
- 既有answer API用例原先由local宿主建任务后用operator接管，现改为operator主体提交；原参数/状态断言保留，新增跨主体403断言。
- 证据目录：[.planning/2026-10-23-aln05-integration](../../../.planning/2026-10-23-aln05-integration/)。最终固定验证见下方记录。

## 回滚与后续

回滚应用到04de43f前须先备份数据，停止新任务；新表和JSON字段为增量，旧程序会忽略新身份边界并恢复宽泛读写，因此不可将切回旧二进制当作保持隔离的安全回滚。需要保持隔离时应恢复本卡代码并由宿主显式修正绑定策略或撤销Grant。

本卡交付服务端ReAct实际记忆装配和所有结果沉淀的身份接线；不声称DAG没有模型装配的节点产生了模型上下文清单，不改变其工具授权。完整多用户登录/IAM、逐运维端点的租户化、Android离线缓存与失效、实际隐私仓储导出删除、生产检索收益和真实供应商均另验。没有新增真机验收。

审阅结论：待独立审阅；05-M原交付及本轮owner/清单修复同样不自批。整包结论：ALN-05仍实现中。下一张串行队列为ALN-11-W。

## 最终固定源码验证

源码commit：`05a7cf43f599a25c6add290063e19f16ae942c49`；tree：`0a0ea440ecc5a1a901b9544b1596f962ffaaff3a`。最终固定Git archive后端 **1460 passed / 1 skipped**（线程异常警告升级错误）；声明、台账、认领表、缺口映射及89主题/20系统用例文档覆盖校验全部通过。新增44项消费者/探针；早期组合328通过，后续Legacy/访问边界组合114通过。

另外将三个后续确定性用例（旧孤立run入口、过时legacy快照、并发业务状态保留）复制到5847f3f原源码副本，**3 failed / 37 deselected**；最终源码对应用例通过。这些记录与04de43f的4项失败探针分开，不把同一阶段测试混计。

客户端Git tree完全未变，复用8c479b6的166 passed/typecheck证据，本轮未重跑客户端全量或真机。日志、命令、退出码及SHA-256在committed-evidence.json和supplemental-evidence.json；文档提交后由verify_metadata.py核对Git blob字节、源码路径不变及固定副本元数据。
