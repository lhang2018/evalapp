# 发现

- .codegraph不存在，跳过；AGENTS搜索无结果。HEAD ea5fc15；07-P已接受。07-B由claude-main交付827d46d，分配codex-runtime审阅。17-A正由claude-main复核，保留其在途文件。
- 07-B新增可选ActionBindings注入；引擎按active_step对result.evidence.effect_id绑定。需验证真实App是否装配、重试/幂等返回/无关工具和多运行计划是否可产生错误归属。
- 20-A为zcode-main新交付，本轮先审07-B实际执行路径，不扩大接受范围。
- 16项实际网关/SQLite/App探针在原实现15失败/1通过：租约下幂等/冲突/异常留下事务、跨连接竞争抛SQLite异常、tool_id冲突被忽略；引擎将无关/无提示步骤、仅run范围及伪造effect_id当确定归属；缓存回执可被新步骤认领；LeaseLost被best-effort吞掉；App主链完全未装配。
- workboard未提交差异为JSON缩进格式，语义与HEAD一致。保留同文件并发更新，收尾时只做语义合并。

- 最终固定源码cc0aa3a全量1653/1、客户端207/typecheck及五检查通过；19探针原代码15失败/4通过、相关241通过。记录六组修复但07-B仍review，保留07-P/13-A/17-A独立接受和11-W/12-A自动化/真机边界。
