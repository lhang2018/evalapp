# 发现

- 运行授权07-C、组合恢复04-V、执行模式08-W、用户控制23-E均有后续实现；先验证动作准入/计划修订/恢复/路由的主链一致性。
- 当前release生产签名相关文件dirty，不混入本轮；其他团队新增资源/知识模块未提交，不视为已交付审阅。
- 已定位待复现：ArtifactStore.save未限制task/node路径，文件名不带digest导致覆盖历史；verify/read接受根外路径且重复读取存在校验间隙；二进制preview字段标base64却返回hex。
- 07-C意图补绑按“每意图唯一候选”推断，两个步骤竞争同一effect会先误绑再冲突；resolve_intent先commit绑定再回填，故障时出现半绑定；record_intent无fencing且可commit外部事务。
- 08-W把模型调用与工具调用余量取min，但候选成本是节点工具次数：模型预算用完会拒绝仍可完成的纯工具工作流。预算未知/零上限/不合法值也需要明确语义。
- 客户端控制：权威终态+本地NEEDS_RECONCILE残留仍显示确认重提；安全恢复未拦prepared/dispatched等账本未决态。预算视图对未知上限写“充足”，并无条件写artifactsPreserved=true，重复上批证据失真风险。
- 核对权威账本后更正：真实未决态是planned/submitted/unknown，未知枚举也应拒绝；旧测试使用的verified不是账本终态，改为confirmed测试正例。
- _budget_state还以max_tool_calls键读取usage，实际字段tool_calls被忽略；已按工具维度读取实耗、保留零上限、缺失/无效预算拒绝为未知。
- _build_retry遗失target/expected_output/required_tools/permissions等字段及principal，自动再发专家大范围授权；已保留契约与原授权上限，自动授权链逐动作检查祖先撤销/替换/过期，显式新授权仍可取代自动委托。
- 首轮Python16探针15失败/1通过；后续加强两节点预算后确认模型/工具维度混用。当前相关100通过，客户端16通过及tsc通过；最初两项故障注入需保留public bind钩子，在拥有的原子事务内调用，未弱化测试。
