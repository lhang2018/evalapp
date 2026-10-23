# WorkBuddy 2026-10-23 工作复核与待修复项

- 复核人：claude-main（本轮会话）
- 复核对象：WorkBuddy 主会话 2026-10-23 的三项工作（ALN-15-A 独立验收、ALN-11-R/16-R 第三方复核、ALN-07-P 复核请求登记）
- 复核日期：2026-10-23
- 结论：**三项均已完成，核心结论可独立复现，无阻断性错误**；下列 7 项为证据链/台账/文档层面的待修复项，
  **已于 2026-10-23 全部处理完毕**（逐项「处理结果」见第二节，复验见第五节）。

## 一、独立复验结果（干净 worktree，未使用工作区在途内容）

| 复验项 | 方法 | 结果 |
|---|---|---|
| ALN-15-A | `git worktree add 6976662 --detach`，重跑 `.planning/2026-10-23-aln15-review/probe_aln15.py` | 43/43 passed，与报告一致 |
| ALN-15-A 回归 | 同一复本跑 `tests/test_budget_reservations.py test_budget_hierarchy.py test_budget.py` | 56 passed |
| ALN-11/16 修复后 | `aae0f9b` worktree 跑 `probe_aln1116.py` | 14/14 passed |
| ALN-11/16 修复前 | `03600e1` worktree 跑同一探针 | 2/14 passed（12 项失败，判别力成立） |
| ALN-11/16 服务端回归 | 把修复后的 `tests/test_aln16_acceptance.py` 拷入修复前复本 | 14 failed；修复后 14 passed |
| ALN-07-P | 当前工作区 `pytest tests/test_plan_revision.py`；核对 `planner.py`/`engine.py:294`/`af6ef48`/`6d55756` | 14 passed；代码与提交归属均属实 |

未复跑客户端 node 测试（需 node_modules）；客户端结论仅由已提交探针脚本佐证。

## 二、待修复项（建议 WorkBuddy 按序处理）

### F-1 证据路径写错（中）
- 文件：`docs/协作开发/验收/2026-10-23-ALN-15-A独立验收.md` 第 8 行
- 问题：引用的 `.planning/2026-10-23-aln15-review/regression-output.txt` 不存在。
- 修复：把实际回归日志 `.planning/2026-10-23-aln15/regression.txt` 复制为 `regression-output.txt`，或把引用改成实际路径。
- 验收：引用路径在仓库中可打开。
- **处理结果（2026-10-23，claude-main）**：在干净复本补跑并落盘 `regression-output.txt`（141 passed）与
  `collab-base-output.txt`（39 passed），报告第 8 行与 §三 均已引用；路径可打开。

### F-2 关键计数缺少落盘日志（中）
- 文件：`docs/协作开发/验收/2026-10-23-ALN-11-R-ALN-16-R独立验收.md` §一
- 问题：以下数字无对应日志文件（证据目录只有 `probe-fixed.txt`、`probe-prefix.txt`、`fullsuite-fixed.txt`）：
  - 服务端新增回归：修复前 14 failed / 修复后 30 passed
  - 客户端回归：修复前 11 failed / 修复后 48 passed
  - 客户端 `tsc --noEmit` exit 0
- 修复：补跑并把输出存到 `.planning/2026-10-23-aln1116-review/`（建议 `server-regression-prefix.txt`、`server-regression-fixed.txt`、`client-prefix.txt`、`client-fixed.txt`、`typecheck.txt`），在报告 §一 引用；若不再补跑，则把表述降级为“转录记录，无落盘日志”。
- 验收：报告里每个计数都能指向一个已提交的日志文件。
- **处理结果（2026-10-23，claude-main）**：已全部补跑落盘——服务端 14 failed → 14 passed
  （`server-regression-prefix/fixed.txt`）；客户端 `artifact.test.ts` 10 passed/13 failed、nativeArtifact 加载失败
  （`client-prefix-artifact.txt`、`client-prefix-native.txt`）→ 全套 48 passed（`client-fixed.txt`）；
  `tsc --noEmit` exit 0（`typecheck.txt`）。报告 §一 已按实测更正原「11 failed / 30 passed」表述并附日志列。

### F-3 新风险未回填台账（中）
- 文件：`docs/协作开发/workboard.json` → ALN-16-R（必要时 ALN-11-R）
- 问题：`from_run_event` 收紧校验后，历史「restricted 且带未脱敏载荷」事件会从静默通过变成显式报错。该风险只在验收报告 §五.3，两个 dispatch 的 `next_step` 均未收录。
- 修复：在 ALN-16-R 的 `next_step` 增加“运行时接线前排查 restricted+未脱敏历史事件路径”；重跑 `python tools/check_collaboration_plan.py --render`。
- 验收：四校验器全绿，`next_step` 可见该风险。
- **处理结果（2026-10-23，claude-main）**：ALN-16-R `next_step` 已追加该风险；ALN-11-R 的计数同步更正为
  「13 项失败 + 1 套件加载失败」；`check_collaboration_plan.py --render` 已重跑（dispatches 36、errors []），
  其余三校验器见下「修复后校验」。

### F-4 ALN-15-A 审阅人被单方面替换（治理项，需用户裁决）
- 文件：`docs/协作开发/验收/2026-10-23-ALN-15-A独立验收.md` §六 + workboard ALN-15-A
- 现状：原登记 reviewer=`opencode-evo`，WorkBuddy 改为 `claude-main` 并直接 accepted。
- 分析：独立性成立（作者 codex-runtime），探针可复现；但替换了既定第三方角色，属流程变更。
- 待办：由用户二选一 ——（a）保留 `claude-main` 的 accepted 结论并在工作包注记原因；（b）恢复 `opencode-evo` 为 reviewer，把 `probe_aln15.py` 与结论交其复核，期间状态回 review。
- 验收：工作包与 dispatch 的 reviewer/status/review_result 三者一致，且有裁决记录。
- **处理结果（2026-10-23）**：用户指示直接修复、不改派审阅 → 采用（a）。裁决记录写入
  `2026-10-23-ALN-15-A独立验收.md` §六（含理由与探针复用路径），workboard ALN-15-A `next_step` 同步注记；
  reviewer=`claude-main`、status=`accepted`、review_result=`accepted` 三者一致。

### F-5 残留 worktree（低）
- 现状：`E:/evalapp-review-aln1116`（`aae0f9b`）仍注册在 `git worktree list`，工作区干净。
- 修复：`git worktree remove --force E:/evalapp-review-aln1116`；确认 `git worktree list` 只剩主工作区。
- 注意：删除前确认无未提交内容（复核时已确认干净）。
- **处理结果（2026-10-23，claude-main）**：`git worktree remove --force E:/evalapp-review-aln1116` 已执行；
  另清理 3 个验证用临时复本（fix-aln15 / fix-aln1116-fixed / fix-aln1116-prefix），
  `git worktree list` 现仅剩主工作区 `E:/evalapp`。

### F-6 文档与记忆乱码（低）
- 位置（两处均为 UTF-8 替换字符 U+FFFD 残字）：
  - `docs/协作开发/验收/2026-10-23-ALN-11-R-ALN-16-R独立验收.md` 第 5 行：「第三方」被写坏
  - `.workbuddy/memory/2026-10-23.md` 第 78 行：「修复」被写坏
- 修复：已改回“第三方复核”“修复不是我做的”。
- 验收：两文件内不再含 U+FFFD 替换字符（`grep -P "\x{FFFD}"` 零命中）。
- **处理结果（2026-10-23，claude-main）**：两处均已改回；五个相关文件（含本复核文档、两份验收报告、
  两份记忆文件）按 U+FFFD 扫描均为 0 命中。

### F-7 长期记忆未收录本轮方法论（低）
- 文件：`.workbuddy/memory/MEMORY.md`
- 问题：MEMORY.md 停在 08:45，未收录“第三方复核对修复前/后两个 worktree 做对照”的方法与本次复验数字。
- 修复：在 MEMORY.md 增加一行索引，指向 `2026-10-23.md` 的“第三方复核 ALN-11-R / ALN-16-R”一节。
- 验收：新会话读取 MEMORY.md 能定位该方法。
- **处理结果（2026-10-23，claude-main）**：MEMORY.md 新增「第三方复核对法」一节，指向
  `2026-10-23.md` 同名小节的「复核方法：修复前/后对照」，并简述判别力要求与日志落盘规则。

## 三、WorkBuddy 复核时可直接复用的命令

```bash
# ALN-15-A
git worktree add --detach /tmp/verify-15 6976662
cp .planning/2026-10-23-aln15-review/probe_aln15.py /tmp/verify-15/
(cd /tmp/verify-15 && python probe_aln15.py)   # 期望 43/43

# ALN-11/16 前后对照
git worktree add --detach /tmp/verify-fixed aae0f9b
git worktree add --detach /tmp/verify-prefix 03600e1
cp .planning/2026-10-23-aln1116-review/probe_aln1116.py /tmp/verify-fixed/
cp .planning/2026-10-23-aln1116-review/probe_aln1116.py /tmp/verify-prefix/
(cd /tmp/verify-fixed && python probe_aln1116.py)    # 期望 14/14
(cd /tmp/verify-prefix && python probe_aln1116.py)   # 期望 2/14

# ALN-07-P
python -m pytest tests/test_plan_revision.py -q      # 期望 14 passed
```

## 四、不在本次复核范围

- WorkBuddy 未在本文档声明的其它操作（`.workbuddy/memory` 以外的会话内容）无法核对。
- 客户端测试原复核时未复跑；F-2 修复时已补跑（前缀/修复后对照见 `.planning/2026-10-23-aln1116-review/`）。
- ALN-11/16 真机与生产侧事项仍按其报告 §五 跟踪。

## 五、修复后校验（2026-10-23，claude-main）

- `tools/verify_claims.py`：0 判错，台账声明全部通过。
- `tools/scan_all_ledgers.py`：0 问题（语义 1 / 裸引用 50 / 锚定 113 条已核对）。
- `tools/check_gap_closure.py`：0 漏项，全部未完成主题均已落到特性。
- `tools/check_collaboration_plan.py --render`：`ok=true`、`errors=[]`、dispatches 36，认领表已重生成。
- 客户端复跑（worktree 前缀 `03600e1` → 修复后 `aae0f9b`）：`artifact.test.ts` 10 passed/13 failed + nativeArtifact
  加载失败 → 全套 48 passed；`tsc --noEmit` exit 0。服务端回归 14 failed → 14 passed。
- 收尾：`git worktree list` 仅剩主工作区。

## 六、本次修复的提交范围

- 文档：本复核文档、ALN-15-A / ALN-11-R-ALN-16-R 两份验收报告。
- 台账：`docs/协作开发/workboard.json`（ALN-16-R / ALN-11-R / ALN-15-A 的 next_step）及重生成的认领表。
- 证据：`.planning/2026-10-23-aln1116-review/` 新增 9 个日志、`.planning/2026-10-23-aln15-review/` 新增 2 个日志。
- 记忆：`.workbuddy/memory/MEMORY.md`、`.workbuddy/memory/2026-10-23.md`。
- 未推送；未改动他人在途源码文件。
