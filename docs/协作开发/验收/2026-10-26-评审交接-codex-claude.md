# 评审交接（codex-runtime 技术复核 → claude-main 终审）· 2026-10-26

交接人：dsh-lead（各卡作者/一审人）
交接对象：**codex-runtime（第一道独立技术复核）→ claude-main（最终审核接受）**
机器可读包：`.planning/2026-10-26-review-handoff/review-package.json`
基线：`published`（`21899e6`）不变；作者不自批；本交接不改变任何卡当前状态。

## 一、流程约定

1. **codex-runtime** 逐卡按下方命令与证据复核；发现问题可直接修复（需在台账登记修复提交/证据）。
2. 复核通过（或无阻断）后，**claude-main 做最终审核**并决定是否转 accepted；未终审前卡不再作为最终发布依据。
3. 任何"组件通过"都不关闭父包/整包；已知缺口保留在案。
4. 若复核人不同意修复范围，可在台账 `next_step` 写明并退回 dsh-lead。

## 二、待复核卡清单（12 张）

| 卡 | 提交 | 复核阶段 | 范围 | 验证命令 | 证据 | 已知缺口 |
|---|---|---|---|---|---|---|
| ALN-30-B | `1b590a3` | 一审 | 客户端成果面板+记忆控制（api.ts/App.tsx/features） | `cd android-app && npm run test:offline && npx tsc --noEmit` | `.planning/2026-10-26-aln30b/api-contract-probe.json` | 项目/节点视图未接线、无全量下载、真机未采 |
| ALN-24-F | `2e0c857` | 一审 | 多主体记忆策略/迁移隔离（memory/scope_policy.py+api） | `python -m pytest tests/test_multi_principal_scope.py -q` | `.planning/2026-10-26-aln24f/scope-policy-evidence.json` | 真实多用户鉴权联调、批量迁移、检索端到端 |
| ALN-16-Q | `aa19558` | 一审 | 聚合观测/受控导出删除 API（api/evidence_api.py） | `python -m pytest tests/test_evidence_api.py -q` | `.planning/2026-10-26-aln16q/api-evidence.json` | 大导出/清理期、跨库删除原子性、设备 |
| ALN-13-S | `0bf8e05` | 一审 | 调度宿主（scheduler/app/cli/api） | `python -m pytest tests/test_goal_scheduler_host.py -q` | `.planning/2026-10-26-aln13s/scheduler-host-evidence.json` | 生产长跑/自动拉起、多进程压测 |
| ALN-09-I | `1d758e9` | 一审 | 候选宿主评测+队列管理（integration+api） | `python -m pytest tests/test_candidate_host_api.py -q` | `.planning/2026-10-26-aln09i/host-evaluation-evidence.json` | candidate/dataset 权威绑定、生产并发、CLI/App |
| ALN-18-H | `f43c8ef` | 一审 | 编程专家按项目安装（coding_consumer.py+app） | `python -m pytest tests/test_coding_expert_consumer.py -q` | `.planning/2026-10-26-aln18h/coding-consumer-evidence.json` | 真实项目、任务→专家自动选择、升级回滚 |
| ALN-19-A | `f772860` | 一审 | 音视频供应商契约切片（tools/media_vendor.py） | `python -m pytest tests/test_media_vendor.py tests/test_release_evidence.py -q` | `.planning/2026-10-26-aln19a/media-vendor-evidence.json` | 真实凭据/预算、费用持久账本、真实协议 |
| ALN-27-C | `79cc06b` | 一审 | 知识持久化/注入/迁移（knowledge+context+app） | `python -m pytest tests/test_knowledge_persistence.py tests/test_knowledge_review_20261026.py -q` | `.planning/2026-10-26-aln27c/knowledge-persistence-evidence.json`；`.planning/2026-10-26-knowledge-review/` | 用户/项目身份可见性绑定、真实前后质量、多进程/逆向迁移 |
| ALN-15-E | `cefdc7c` | 一审 | 原生采样+后台准入（Kotlin/TS/backgroundRunner） | `cd android-app && node --test test/device/resourceSampler.test.ts && npm run test:offline && npx tsc --noEmit`；Kotlin：`gradlew :app:compileDebugKotlin` | `.planning/2026-10-26-aln15e/client-evidence.json`；`.planning/2026-10-26-proactive-native-review/` | 默认后台路径未注入 sampler/highWater、真机周期与压力、OEM |
| ALN-25-C | `4c2a942` | 一审 | 主动任务准入执行/预留释放（proactive_execution.py+app） | `python -m pytest tests/test_proactive_execution.py -q` | `.planning/2026-10-26-aln25c/proactive-execution-evidence.json`；`.planning/2026-10-26-proactive-native-review/` | App 真实 15-D 决策器/预算账户、并发与运行中 pause/stop、真实 LLM 质量 |
| ALN-30-A | `67bed46` | **二审** | 服务端装配收口（integration/wiring+cli+app） | `python -m pytest tests/test_integration_wiring.py tests/test_restore_entry_review_20261026.py -q` | `.planning/2026-10-26-dsh-review/review-probes.json`；`docs/协作开发/验收/2026-10-26-dsh独立复核-25B-30A.md` | host-restore 成功链路需真实部署实例补证；`621a191` 回退路径 |
| ALN-25-B | `704ec42` | **二审** | 主动强度/停止/资源非干扰（goals/proactivity.py） | `python -m pytest tests/test_proactivity.py tests/test_resource_admission.py -q` | `.planning/2026-10-26-dsh-review/25b-real-resource-pressure.json`；`.planning/2026-10-26-windows-second-review/` | 真实设备 UI、执行层 submit 接线；`5bc1725` 采样未知/恢复修复需复核 |

## 三、全局命令

```powershell
python -m pytest tests/ -q                     # 后端全量
cd android-app; npm run test:offline           # 客户端全量
cd android-app; npx tsc --noEmit               # 类型检查
```

## 四、复核重点（按最近复核动态）

- 25-C/15-E：`6e82856` 已修 PX-01~04 + NR-01~02；剩余"宿主配置（预算账户/真实 sampler）""15-E 默认后台路径注入""真机压力"是**接受阻塞点**，不是回归。
- 27-C：`a0de595`/`36d078a` 已修 KR-01~07；正式验收项 2（用户/项目身份可见性）仍未实现，不可当作已关闭。
- 30-A：一审已过；二审重点补 host-restore 成功链路（需已部署实例）。
- 25-B：一审（dsh-lead）曾通过，`5bc1725` 后需二审复核采样未知/恢复语义。
- 09-I/13-S/18-H/19-A：`0ba37e1`/`01766f5`/`fde8f89` 已修复各自复核问题，需确认修复后边界仍如实。

## 五、交接方式

- 本文件与机器包已提交到仓库；外部会话（codex-runtime / claude-main）可直接读取本文件、台账 `next_step` 与 `.planning/2026-10-26-review-handoff/review-package.json` 开始复核。
- dsh-lead 无法直接向外部会话发消息；请用户在对应会话中指示其按本交接执行。
