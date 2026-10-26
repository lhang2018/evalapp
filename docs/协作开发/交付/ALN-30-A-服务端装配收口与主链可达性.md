# ALN-30-A｜服务端装配收口：零入口域接线与主链可达性

状态：**review，codex技术复核完成、独立终审待分配**。原dsh-lead一审接受保留历史；原终审claude-main与作者冲突已清空。codex不自批此前自己写的恢复补丁。

2026-10-26指派二审：真实空部署实例、任务/事件/记忆三库写入及备份后变更，经CLI host-restore成功恢复内容/计数和实例身份，原“成功链路需实例补证”在本地宿主范围已补齐。固定f973dcc本卡相关38项通过；全13项相关522通过/1整机饱和未跑，不是生产部署或整包接受。详见[13项复核](../验收/2026-10-26-指派十三项技术复核.md)。

2026-10-26行为复核增量：621a191修复host-restore漏拦未结束任务、旧恢复入口只看最近1000任务、真实恢复失败未回退三项问题。固定归档相关291过，新增修复待复核；原交付/变异记录保留，不能以原27项通过代替新边界验证。详见[台账与恢复入口复核](../验收/2026-10-26-他人验收台账与恢复入口代码复核.md)。
交付提交：`67bed4644a84fd564a0e752ac4bc06b4f7ba3382`（台账delivered_commit已补）；本卡为ALN-30首卡。

2026-10-26接续状态核对：本轮固定0e1b4fd相关176项通过，保持review。下文“四域均零引用”是勘察口径错误：feedback_api此前已有observability.feedback引用；本卡接入的是具体声明能力与凭据CLI，不覆盖16-P聚合观测API。原2299/1日志缺精确Git树绑定，当前tests仅采集2328项，不能直接作为当前版本完整全绿；原数字及清理守卫异常保留为历史证据。详见[本轮状态复核](../新增状态复核与需求拆分_2026-10-26.md)。

## 一、问题：已交付但用户不可达

2026-10-26 勘察（写入需求拆分 plan 的卡 goal）：`evoagent` 24 个一级域中
**deployment / recovery / observability / knowledge 四域在 app.py、cli.py、
api/ 三处均为零引用**——17-P（部署/恢复）、29-C（凭据）、27-A（知识库）已
通过独立复核，但用户在运行主链上触达不到。另有交付报告点名的装配缺口：
25-A 的 `ProactiveAssistService`（交付时明确把装配留在保留缺口）、11-P 的
`IdentityRegistry` 未注入 `ReleaseDelivery`（端到端不生效）。

27 份交付报告提到 app.py/App.tsx，其中至少 6 张明确声明装配缺口——这是
并行开发的系统性代价：每张卡都回避共享集成文件，能力就悬空在库层。

## 二、交付内容

### 1. 新增 evoagent/integration/（独占模块）

- `wiring.py`：能力→入口的可校验声明（`CAPABILITIES` 六项，每项带来源卡）；
  `audit_wiring()` 审计三面入口（app 属性 / CLI 命令 / API 路由）；
  `assert_wiring_complete()` 悬空即抛 `WiringError`，**不允许静默通过**。
  CLI 命令集用 ast 解析 `cli.py` 的 `add_parser` 字面量——新增子命令自动
  被识别，零维护；API 取 `routes.registered()`。
- `services.py`：`DeploymentService`（把 17-P 的模块级函数包装成可装配
  对象）+ `list_identities()`（identity.py 无公开列举接口，受限读取集中
  一处并注明原因；登记簿只存公钥，导出无害）。

### 2. app.py 装配（外科手术式，两处精确点）

- 新增 `identity_registry` 并注入 `ReleaseDelivery`（11-P 缺口闭合）；
- 新增 `deployment` / `recovery` / `credentials` / `knowledge` /
  `memory_control` / `assist` 六个装配属性。既有服务初始化顺序未动。

### 3. cli.py 七个入口

`host-deploy`（空机部署+版本固定校验）、`host-restore`（组合恢复，默认
取最近备份）、`credential-audit`（凭据登记概览+私钥片段检测）、
`knowledge-list`、`assist-organize`（只读整理）、`identity-list`、
`wiring-audit`（可达性审计）。

## 三、证据（对应验收四条）

| 验收项 | 证据 | 结果 |
|---|---|---|
| 四域 CLI 真实路径 | `host-deploy` 真实落盘 `deployment.json`；`host-restore` 完成真实恢复链（`ok:true`/`identity_preserved:true`/`counts_matched:true`/自动安全备份）；`credential-audit` 对 `BEGIN PRIVATE KEY` 片段检出；`knowledge-list`/`assist-organize`/`identity-list` 可用 | ✅ |
| 点名缺口闭合 | `app.delivery.identity_registry is app.identity_registry`；`assist.memory_control is app.memory_control`；审计 6/6 可达 | ✅ |
| 可达性机器校验 | `wiring-audit` 零悬空；`test_audit_detects_capability_without_any_entry` 证明缺入口必报错 | ✅ |
| 失败/恢复/拒绝路径 | 非空目录拒绝部署；无部署清单拒绝恢复（fail-closed）；私钥片段检出、干净文本放行 | ✅ |

- 新增 `tests/test_integration_wiring.py` **27 项**全过；
- 相关既有套件（deployment/recovery/backup/release_install/proactive/
  memory_control/goals）**139 passed** 无回归；
- 隔离 worktree（干净 HEAD+本卡改动）后端全量 **2299 passed / 1 skipped /
  0 failed**（455s）；
- **变异测试 10/10 捕获、0 存活**（`mutation_test.py` + `mutation-results.log`）。
  其中 M4/M7/M9 首轮存活暴露三个盲区（审计"可达"≠"app 装配存在"、声明集
  自身可被削减、bootstrap 拒绝先于 verify 使短路不可感知），已各补钉住
  装配事实/声明集/verify 真实性的测试后全捕获。

### 修复的既有验收切片（1 处）

`tests/test_update_artifact_delivery.py`：App 默认注入 IdentityRegistry 后，
该套件（API serve 走完整装配）的 reconcile 触发 `signing_identity_trusted`
检查而空登记簿判 `unregistered` → `blocked`。修法是**补齐测试前提**：
setUpClass 把其发布身份 `release-key-1` 登记激活——套件语义本就是"可信
身份发布的制品可被对账接受"（断言 `signing_key_id == "release-key-1"`）。
5/5 通过。

## 四、兼容/迁移影响

- `App()` 新增六个属性与一个登记簿文件（`<data_dir>/identity.json`）；
  既有属性名与初始化顺序未动，CLI 新增 7 个子命令——均纯增量。
- **行为变化（有意）**：装配了 IdentityRegistry 后，回执对账会做身份信任
  检查；未登记身份的发布在对账时判 `unregistered` 而 blocked（原来无此
  检查）。这是 11-P 设计语义的激活，不是行为回退；`test_update_artifact_delivery`
  的夹具已按该语义补齐。

## 五、保留缺口（未验证不记通过）

1. **客户端装配不在本卡**：App.tsx 消费面（12-E 在途 review）未动；
2. **knowledge 为进程内存储**（27-A 交付语义）：CLI 只能列出本进程登记
   的条目，跨实例/持久化需后续卡；
3. **25-B 策略层**：主动建议到任务的实际启动装配（消费 may_start 决策）
   仍留 25-B 边界；本卡只接只读分析入口；
4. **deployment 空机语义**：`host-deploy` 面向空机；存量实例的就地升级
   流程不在本卡；
5. **真实 LSP / 真机 / 生产供应商**：18-L、12-E、02-P 各自的原验收缺口
   不因本卡自动关闭；
6. **门禁证据回填**：G0–G7 / R01–R13 的证据采集与发布决定属 ALN-29/
   ALN-22 范围，本卡不触碰。
