# 发现

- MemoryControlService将范围策略存入memory.db独立表，但MemoryStore.search和ContextAssembler均未消费；get/set成功不代表模型遵守。
- 现有scope_allowed语义：scope在allow且不在deny，空allow不隐式变成全允许。管理list/get与运行检索应区分，禁用不删除记忆，用户仍需查看/纠正/删除。
- 统一在MemoryStore.search读持久策略，在相关性召回/向量重建前过滤，覆盖默认App既有assembler，不争用app.py装配。身份与显式Grant约束仍执行；策略不能扩大原有访问。
- 新《待接需求》已存在但不完整，用户要求将识别到的文档/遗留项汇入该页；收尾补来源、实施状态、接受条件与未明确承接项。
- 首轮负向探针11失败/3通过/1排除；新路径修复后先行23通过/1排除。固定c6c9872记忆域+工具209项中208通过，唯一失败为工具测试硬编码“30个工作包”，ALN-30新增后应为31；修为按实际数量校验，不改台账或渲染逻辑。
