# 协作开发入口


2026-10-26核对0e1b4fd新增交付并补拆17卡后，已登记31包、105卡：75 accepted、11 review、0 in_progress、19 unclaimed。见[本轮状态与拆分](新增状态复核与需求拆分_2026-10-26.md)；结构化状态以[workboard](workboard.json)为准，[认领表](开发任务认领表.md)由工具生成。

遗留汇总见[zcode遗留与现状对账](../zcode遗留与现状对账_2026-10-26.md)：zcode名下26卡中20 accepted、6 review。组件接受、整包完成与产品验收分别记录，保留缺口有明确后续完成条件。

共同开发基线已发布为21899e6。首发产品仍为0/17 passed，八道门禁G0—G7均untested；共同开发基线发布不表示后续功能或首发产品已通过。

## 当前安排

ALN-30-A由claude-main交付67bed46，状态review；本轮相关176项通过，精确版本完整交付证据仍待补，不升accepted。新增17卡全部unclaimed，优先30-B客户端接线、16-Q受控观测API，再推进29-F真实成果采证；同一App.tsx/app.py/controller.py接线串行整合。

待审卡为07-C、12-E、12-V、18-L、21-A、25-B、26-A、26-B、27-B、29-C、30-A；新增卡和19-A/22-A均未认领。原owner/reviewer保留，新修复固定提交复核，不能套旧接受结论。

开发优先补服务端装配、记忆范围策略对真实检索的约束、手机主链与第一条真实成果场景；生产配置、资源压力、真机、LSP与知识实际消费按原卡边界逐项验收。固定外部Runtime PoC等已有切片先复用，不再统称未实现。

## 代码与发布现状

- 最近提交代码核验：026ab2b全量2343通过、1台账引用失败；67fcc54仅修引用后，台账与工具85通过。产品/测试/工具子树相同，分层去重覆盖2344项，跳过1项、显式排除1项；未第三次全量。客户端同子树沿用998db6f的424项与类型检查，非本轮重跑。[复核报告](验收/2026-10-26-反馈私钥与模块身份复核.md)。
- 以下f550d90及通知修复数字为历史证据，不能作为当前HEAD或首发17用例的验收结果。固定f550d90归档后端1661/1、客户端228、类型检查、ARM64构建及五项检查通过。[证据](../../.planning/2026-10-24-baseline-device/candidate-validation.json)。
- f550d90版本APK+JS已重新安装到真机，Hermes模块回归9/9：真实存储、下载验签、硬超时、实际强杀恢复、坏包回滚与旧能力执行。动作transport与更新服务为明确夹具，无障碍未授权，不能称点击或自然语言端到端，也不覆盖后来新增的原生通知。[真机绑定证据](../../.planning/2026-10-24-baseline-device/device-attestation.json)。旧04-UI/11-W取证继续保留历史采集归属。
- 12-A接受协议/矩阵；12-E后续本机打开设置链路已有代码与组件证据，固定包真机和通用设备主链仍另验。20-P已有本机真实Ollama PoC，主链采用和长期稳定性另验。
- 07f11e5、532a364等旧候选与通知252项等历史数字均保留各自时点，不替换已发布21899e6或后续增量验证。

## 接手与共享文件

1. 先读任务卡及[共享文件规则](协作规则与共享文件.md)，确认实际代码和已有交付，避免重复开发。
2. 在workboard记录owner、实际branch/base_commit与状态；base_commit是实际接手锚点，尚未发布时必须注明并非已发布基线。
3. 当前多位开发者按任务边界认领；新增接线范围先补卡片和shared_files，共享文件按角色串行合入，不复制第二套授权、Goal、预算或副作用权威。
4. 通知、DevicePackage和backgroundRunner相关实现已在0b8d7dd提交；后续仍保留实际存在的未提交内容，不用重置/清理消除他人工作。
5. 完成后按[交付模板](交付模板.md)记录精确提交、测试、迁移、兼容和未完成范围。review保留独立审阅要求，只有一个开发者不意味着可以自审自收。

重新加入其他开发者时，再根据独占路径安排并行或独立分支/工作树；不能把当前的单会话串行授权推及未来并发覆盖。

## 更新与校验

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
python tools/check_collaboration_plan.py --render
python tools/check_collaboration_plan.py --verify-prepared
python tools/check_collaboration_plan.py --verify-current
python .planning/2026-10-22-design-alignment/verify_alignment_plan.py
python tools/verify_claims.py --check
python tools/scan_all_ledgers.py
```

文档检查器证明映射、认领与引用一致，不证明业务功能完成。发布时以约定范围重跑后端、客户端、类型检查和真实环境用例，逐项绑定同一候选。

### 跑测试时的环境约束（2026-10-25 实测）

- **pytest 只在系统 Python 3.10.11**：`C:/Users/lhang/AppData/Local/Programs/Python/Python310/python.exe`（managed 3.13.x 无 pytest）。`python3` 是 Windows 原生解释器，认不到 `/tmp`。
- **pytest 必须显式带 `tests/` 路径**：主工作区 `outputs/` 下有 62 份历史复核仓库副本，不带路径会收集到 5471 个重复测试文件并报 5471 errors；干净 worktree 无此问题。
- **全量验收固定用干净 worktree**（`git worktree add <dir> <commit> --detach`），避免主工作区的在途改动与历史副本污染结果。
- **后台跑全量把输出重定向到文件**（`pytest tests/ -q > run.log 2>&1`）：pytest 清理自身临时目录会触发环境的批量删除守卫（实测删除量 1867、阈值 50），管道方式可能被中止。

完整条目、取证命令与审计日志查法见[环境异常与规避](环境异常与规避_2026-10-25.md)。

## 设计与历史记录

- [正式版1.0](../EvoAgent_全量详细设计正式版_1.0/)：需求与权威契约。
- [全量实施计划](../全量对齐与下一步实施计划_2026-10-22.md)：23包原始依赖及关闭标准，历史测试数量不代表当前HEAD。
- [2026-10-23现状报告](../项目现状与设计差距核对_2026-10-23.md)：本轮差距与推进顺序。
- [交付目录](交付/)与[验收目录](验收/)：历史源码、缺陷、修复及真实环境证据。

旧F/WP编号仅供历史定位；当前以ALN包和子卡追踪。认领、实现、审阅、整包验收及基线发布分别记录。
