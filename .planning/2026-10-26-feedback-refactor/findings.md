# 发现

- 第五批已修复JSON/别名/集合凭据与作用域/路径/CRLF边界，29-C和18-L仍review。属于本会话历史修复，接续不等于独立接受。
- 当前凭据规则识别完整PEM块，需核查截断的私钥材料；一般结构递归与字符串载荷边界待核查。
- 当前重构为受限AST/symtable分析，不是LSP；仍需验证显式导入别名、绑定与未改写符号之间是否保持语义一致。
- 首轮18项11失败/7通过：截断PEM和完整DSA/ENCRYPTED类型漏检；重复from-import别名与包导出遮蔽会错误生成全局改写。已补私钥标记覆盖及导入/包命名空间歧义拒绝，合法明确模块导入保留支持。
- 实际运行项目补充验证后，额外包属性/__dict__改写探针3失败/22通过，说明仅检查包内Name绑定仍不足；已拒绝可见包属性及导入命名空间前缀重绑、目标符号的字典式改写，不把这些情况假定为静态模块引用。
- 固定全量2341通过但diagnostics_import的415测试遇到Windows连接中止。诊断路径在拒绝Content-Type/角色时未消费小请求体，已有授权路径则用64KiB/0.25s有界丢弃。新增确定性探针两项修前全失败，复用既有helper补齐拒绝路径，不改权限/状态码或放宽断言。
- 用户追加检查zcode自身遗留；现有docs/设计差距三层对账_2026-10-25.md由zcode-main整理，另有根FEATURES/IMPLEMENTATION/GAP_ANALYSIS及任务卡分散登记。先区分zcode人员归属与产品存量缺口，不能把历史问题或accepted卡等同于当前已全部验收。
- 当前90b5c7a已登记31包88卡：75accepted/10review/1in_progress/2unclaimed；zcode-main/next名下26卡=20accepted+6review。30-A由claude-main在途，覆盖服务端装配，不覆盖全部真实消费。当前workboard published=21899e6，与根README/协作README旧candidate口径冲突。
- 固定入口AST审计确认deployment/recovery/knowledge无直接导入，但observability.feedback已有feedback_api三处导入且server已注册反馈端点，30-A“四域均零入口”描述过宽。聚合观测API未注册与反馈入口存在应分开。
- 24-D交付明确记忆范围策略只在控制面过滤，ContextAssembler未读取策略；18-G自动项目装配、27-A真实知识注入与迁移评测、16-P聚合API/端侧事件均有明确遗留。归并12组，按已有卡承接，不重复开发。
- 产品矩阵结构/require-pass退出0/1，门禁结构/require-release退出0/2；当前0passed/17not_run，G0—G7全部untested。17用例映射文末仍建议组件/旧设备证据拼接为integration通过，与文首撤回冲突；已改成实际同版本场景采证。
