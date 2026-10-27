# 初始发现

当前11项指定review已有2026-10-26技术复核，用户要求再review。最新提交主要为07-C/15-D/12-E接续，不以重复旧通过数当新验收。核对源码范围及真实消费者后补边界证据。

他会话在途：Android App/update及对应测试、release/channels.py与测试、workboard的ALN-29-E与派生表、29-E交叉复验文档、UI、matrix等，保留且不混入。

明确新缺陷：18-L分析参数注解时漏掉vararg/kwarg；唯一模块类重命名被批准但字符串前向注解保留旧名，typing.get_type_hints实际报NameError，须与已有字符串注解一样澄清。24-F初始化_ensure无条件commit，会提交宿主父事务；自身建表第3步失败留下前两张表。真实SQLite/跨连接及可执行前后注解新9探针修前8失败1通过。

修复仅补variadic注解扫描，以及_ensure复用现有事务所有权装饰器；普通真实类型注解仍能重命名。探针观察连接补row_factory以便后续principal_policy读取（修前断言在更早in_transaction已失败，未计为产品缺陷）。

定位时installer.py、knowledge/persistence.py、MemoryPanel.tsx、memory_scope_api.py猜测路径不存在，已通过实际文件清单改为coding_consumer.py、knowledge/store.py、features/memory/control.ts和memory_context_api.py；无状态变更。

固定e89b4ae：后端30模块487通过/1整机压力未选，客户端469/TS通过。finalizer初次把pytest deselected误当JUnit skipped断言失败，在写任何报告/台账前中止；改以日志核对deselected，JUnit实际487测试无skip/failure/error。
