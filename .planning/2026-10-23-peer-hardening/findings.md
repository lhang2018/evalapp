# 发现

- 已有7132180/1ff11c1修复存在，不能重复报告为未修。
- 恢复删除文件使用write_text，检查后出现的其他文件可被覆盖；回滚删除文件同样存在。
- WorkflowModeCase.candidates空列表会回用共享候选；重复身份构造matcher在try之外，单条坏数据可中断整批。
- 全部硬检查通过但声明dynamic_nodes的候选被选择为stable，丢失动态节点。
- dataclass frozen不冻结调用方传入的列表/集合；负成本、布尔/小数预算、字符串budget_known等未严格验证。
