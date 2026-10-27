# 发现

当前明确codex11项已完成接续，本轮选择其他五卡。26-A既有domain/intent签名修复、27-A质量清单、27-B精确组合、09-H隔离/队列与10-E基准均有历史复核；进一步核查证据真实性和失效边界，不重复冒称生产接受。

确认10-E三类缺陷：stdout最后坏行会被回溯跳过、NaN/Infinity被当合法JSON；摘要未绑定逐用例status/ok/value及timeout/阈值；release_decision仅排inconclusive，regressed/unknown可在回归列表空时放行，也不查摘要。14新探针修前12失败2通过，真实资产+候选子进程覆盖输出协议；报告修改探针明确仅校验一致性，不冒充签名/来源验证。

修复升级host-bench/3，末行严格JSON与有限浮点；摘要绑定语义case投影及政策/timeout，仍排除时间噪声；门禁核对版本/摘要，且只识别improved/neutral为可放行结论。旧v2需重跑，不改历史证据版本。26-A已在plan_candidates fresh重算比对权威快照，27-A/27-B保留质量归因/生产展开缺口，不重复修已处理问题。
