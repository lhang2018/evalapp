# 反馈凭据与跨文件重构接续复核

用户继续指示，起点06a3cd9；沿用planning-with-files。复核29-C凭据拒绝/导出脱敏与18-L静态归属和改写计划。发现问题直接修复，不重复开发已交付切片，不冒称独立接受。

| 阶段 | 状态 |
|---|---|
| 调用/契约与代码边界 | complete |
| 负向探针与最小修复 | complete |
| 固定Git适当回归 | complete |
| 文档台账、精确提交和缓存回收 | complete |
| zcode存量缺口文档定位与当前状态核对 | complete |
| 补齐可执行清单、验证与提交 | complete |

保留matrix.py、UI/Metro和其他会话文件。测试仅用合成数据，秘密文本不使用真实凭据；临时范围在E盘新建并回收，不删除旧缓存。不操作真机或真实外部服务。

## 错误记录

- 初次检索不存在的evoagent/feedback目录；实际实现位于evoagent/observability/feedback.py，已按现存路径读取。
- 首轮负向探针11失败/7通过，确认PEM漏检与模块身份误归属，原日志保留。
- 额外命名空间修改探针3失败/22通过，修复后最终新增27项与原相关回归83项通过。
- b5b376d全量2341通过/1失败/1跳过/1排除；失败为diagnostics_import拒绝Content-Type时WinError10053。保留失败证据，核查有界拒绝体丢弃路径，不以普通重跑掩盖。
- 026ab2b第二轮完整2343通过/1台账引用失败/1跳过/1排除，HTTP故障已消失；IMPLEMENTATION/FEATURES旧server.py:1141落到空行。改为实际_evolution_propose符号锚点，不再猜偏移行号；文档与工具定向复检，产品/测试代码保持相同，不重复第三次运行未变化的程序套件。
- 遗留检查初次按workpackage.status取值报KeyError；父包字段实际delivery_state，改读既有结构，不修改台账schema。PowerShell不展开rg路径通配符，改用-g过滤；两处猜测文件名不存在，改为rg --files定位。
