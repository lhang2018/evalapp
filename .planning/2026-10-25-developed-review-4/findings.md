# 发现

- 四张卡均review，组件均声明主链/生产装配仍待后续；优先审查预算、状态推进、证据真实性和版本固定。

- 27-B compose未调用IO检查，且从Asset不存在的属性读IO（真实数据在content）；结果只拼新ref未注册，根资产还被从依赖集移除，不能精确消费。闭包跳过visited导致成环未拒绝。
- 26-A分析结果可由调用者改写且只查引用存在，空引用/不足/成功任务伪证也可产生候选；goal投影与原失败task会重复当独立证据。
- 25-B off状态record_attempt/status访问缺失NO_PROGRESS_LIMIT键；调档未重置停止锁；负数/NaN并发不拒绝。
- 26-B实际推进/动作准入不校验契约；writer映射可漂移，部分角色失败仍可由另一角色完成全局；handoff校验漏输入/预算/范围，列表浅冻可被外部修改。
- 精确实现提交与文件：26-A=`662c258`（`goals/gap_planner.py`）；25-B=`704ec42`（`goals/proactivity.py`）；26-B=`37465ab`（`experts/team.py`）；27-B=`3a716d0`（`assets/composition.py`），各自测试文件独立。
- 验收关键约束：25-B 要求资源压力实测、主动失败不改核心状态、关闭即停；26-A 要求真实证据可追溯/去重/人工确认且不绕过评测发布；26-B 要求唯一状态写入者、冲突可解释、局部失败不越权重放；27-B 要求依赖闭包、IO兼容、真实执行证据和运行版本固定。
- 当前 HEAD=`37928a9`，目标实现文件均无工作区修改；仅 release/delivery.py、release/manager.py、runtime/matrix.py 与 Windows 测试等属于其他会话在途，保持排除。
- 三路复审一致候选：25-B 的 off 失败计数 KeyError、预算/并发仅预检且数值边界不严、停止锁可通过调档绕过；26-A 信任调用方可变 analysis、证据仅查存在且自动创建候选；26-B 角色/交接结构浅冻结、writer/actor/推进目标校验不足；27-B 依赖环未拒绝、compose 未执行 IO 检查且从错误字段读接口、返回组合未注册为可消费资产。
- 附加边界：四个新组件目前主要被测试直接调用，生产 App 尚未装配；审计通过只能证明组件契约，不能声明生产主链完成。
- 八组候选已逐项验证为 CONFIRMED：①off 下失败记录 KeyError 且调档可绕过停止；②主动预算/并发仅非原子预检并接受负数/NaN；③26-A信任调用方 analysis、只查证据存在且无候选创建确认；④TeamRole/state_writers 可后验修改实现动作/写者提权；⑤advance 的 actor 仅记录、任一writer可推进全局且可忽略其他角色失败；⑥handoff 接受 NaN/inf/坏元素且浅冻结；⑦依赖闭包不拒绝环；⑧compose 不调用 IO 检查、读错 Asset.content、未形成可注册/消费组合。
- 验证代理观察到 `team.py` 已出现未提交加固迹象；修复前必须刷新 HEAD/diff，区分并发已吸收项与仍缺失项。
- 刷新后 HEAD=`362a9a9`；并发工作树已修改 `team.py`（角色/交接 tuple 化、writer 映射复验、handoff 数值/上下文校验、全局失败阻止完成）和 `proactivity.py`（off 安全、并发数类型校验、资源准入 fail-closed）。这些改动尚未提交，归入本批审查修复继续验证。
- 仍未吸收：25-B 预算/并发原子预留；26-A analysis/证据真实性；26-B actor 与具体状态物授权语义；27-B 环检测、IO读取/校验与组合注册消费语义。
- 读取当前代码后校准：26-A 已通过重新 analyze + `_snapshot` 等价比较、空引用拒绝、候选去重修复调用方篡改/陈旧证据；候选创建仍不要求当场人工确认，但后续评测/发布门禁保留，是否要求创建确认属于产品口径而非直接安全绕过。
- 25-B 已修 off KeyError、调档重置、非负整数并发校验和资源准入 fail-closed；预算/并发原子预留仍是生产装配缺口，模块文档明确当前是纯决策层且无生产调用者。
- 26-B 已修浅冻结、writer 映射漂移、handoff 非有限预算/输入/上下文、其他角色失败时全局完成；actor 认证和按具体 artifact 推进仍缺运行时身份/状态模型，当前无生产调用者，应避免在纯契约层伪造认证。
- 27-B 当前实现尚未修复：visited 仍吞环、compose 不读 `Asset.content`、不调用链式 IO 检查、可用 override 绕过坏链、返回值不登记仓储；新审查测试已明确要求这些行为。
- AssetRepository 可直接登记 draft，并在后续 approved/active 迁移时复用 `_assert_acyclic` 与摘要 pin；组合实现应先完整验证所有根资产 active/approved、递归环、相邻真实 IO，再以根引用为 dependency_refs、根 digest 为 compatibility pins/正文证据登记 draft。override 只能覆盖最终公开 IO，不能跳过底层链兼容检查。
- 最新并发重写版 composition 已采用迭代环检测、真实 `Asset.content` IO、严格链检查、approved/active+评测证据约束、不可伪造 override 和 draft 仓储登记；`project` 属合法 namespace，共享数据库锁为可重入 RLock。当前相关 41 项通过。
- 审查结论边界：25-B 原子预算/并发需装配层 reservation/dispatch；26-B actor 身份与逐 artifact 完成需运行时 principal/状态模型；四组件当前均无生产调用者。把这些写进保留缺口，不在独立模块内伪造认证或半套资源预留。
- 最终修复提交为 `6ae4b30`，固定后 79 项第四批定向回归通过；UTF-8 + E盘专属临时目录执行完整 `pytest tests -q` exit 0；协作元数据、实现声明和台账行号扫描全部通过。
- `6ae4b30..HEAD` 的四实现与审查测试无差异；最新台账已将 25-B/26-A/26-B/27-B 全部置回 `review` 并指向固定修复提交，避免对新增修复自审 accepted。
- Ruff 严格检查仍报告导入排序/旧式 typing 等非行为风格项；未为审计通过做大规模类型注解现代化。删除了本轮专属 pytest 临时缓存 `outputs/review4-pytest-temp-362a9a9`（959MB），未清理用户或其他会话缓存。
