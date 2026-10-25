# evalapp 项目长期备忘（跨会话）

## 当前协作格局（2026-10-23 起）
- 用户指定**只有 codex-runtime 一个开发会话**，串行推进、不另建工作树；已接手 12-A/14-A/15-A/
  17-A/20-A/00-B 及新卡 05-I/11-W，下一张 ALN-15-A，wip_limit=1。claude-main（WorkBuddy 主会话）
  侧负责台账刷新、验收/复核登记与检视。
- review 纪律不变：审阅人须与作者不同；只有一个开发者也不自审自收。
- 他们随时可能在共享文件留大量未提交改动——**动任何共享文件前先 git status**。

## 只提交自己改动的外科手术式暂存（并行仓库必备）
对方在途时直接 `git add <共享文件>` 会把对方改动扫进自己提交。正确做法：
`git show HEAD:<file> > tmp.json` → 在 tmp 上只做自己的编辑 → `git diff --no-index` 验证差异
恰好是自己的几行 → `git hash-object -w` + `git update-index --cacheinfo`（只改 index，不碰工作区）
→ 提交后 `git show HEAD:<file>` 三连核对（自己行在内/对方行不在/对方改动仍留在工作区）。

## 并行工作流现实（最高优先级）
- 仓库有并行工作流在持续提交，**改动毫无预告出现**。看到未提交改动先 `git status` 确认归属，别默认是自己早前的残留。
- tsc/测试结果**只在「跑那一刻的工作区状态」**上成立；自己正改文件时跑出的错误不能当基线。
- 判定"是不是我的问题"用隔离验证，不靠猜：`git worktree add <tmp> HEAD --detach` 干净复本（比 stash 安全，不碰对方工作区）。
- 并行会话会把本方暂存文件扫入其提交 → 提交后核对 `git show HEAD:<file>` 确认关键改动在内。
- 在途领域（勿触碰）：goals 域/authorization.py、controller.py、app.py、Android 通知、客户端文件等，以 git status 实时为准。

## 偶发测试失败甄别协议
- `tests/test_process_recovery.py`（`_kill_at_boundary` 固定 sleep 0.85 后击杀子进程）与
  `tests/test_run_ownership.py` 是**时序敏感**测试，机器高负载（尤其并行会话同时跑 pytest）时边界漂移 → 偶发失败。
- 协议：单次失败不作数 → 重跑一次 → 仍不过再用 worktree 隔离验证 → 才可记录为真实问题。
- 历史实例：组合运行 7 失败，重跑 89 全过；单独重跑两文件 15/15 过（2026-10-22 记录）。

## 环境备忘
- pytest 只在系统 Python 3.10.11：`C:/Users/lhang/AppData/Local/Programs/Python/Python310/python.exe`（managed 3.13.12 无 pytest）。
- `docs/` 与 `资料/` 下文件 shell 的 `cat`/`wc` 会被沙箱拦截，必须用 Read 工具读。
- 跑测试用 vitest（客户端 node --test），python3 是 Windows 原生 Python，认不到 /tmp。

## 认领流程（唯一来源）
README → `docs/协作开发/README.md` → `docs/协作开发/workboard.json`（`owner=null` 即未分配）。
改 workboard.json 后必须跑 `python tools/check_collaboration_plan.py --render` 重生成认领表，否则校验报 stale。

## 校验器（改台账后必跑）
- `tools/verify_claims.py`：台账声明机器校验（0 判错）
- `tools/scan_all_ledgers.py`：全台账引用漂移扫描（0 问题）
- `tools/check_gap_closure.py`：主题→WP→特性三段反查（0 漏项）
- `tools/check_collaboration_plan.py`：协作计划校验
- 台账数字绝不手工估算；测试计数是提示级不拦截（并行开发必然滞后）。

## 第三方复核对法（2026-10-23 已落地）
- 方法索引：`.workbuddy/memory/2026-10-23.md`「第三方复核 ALN-11-R / ALN-16-R」→「复核方法：修复前/后对照」。
  要点：`git worktree add --detach` 同时取修复前/后两个提交，自写探针按 cwd 加载源码，
  要求同一探针「修复前失败、修复后通过」才承认判别力；数字一律落盘成日志再写进报告。

## 产品层权威规格（2026-10-25 新增，长期索引）
- **EA-PRODUCT-007 v1.0** = `chat/extracted/156_qa.md`：八模块、**28 个 UX 功能**、能力状态六档、
  主动程度四档、开放顺序六层、每项功能验收八要素。原文可逐字引用。
- **EA-UX-008 v1.0** = `chat/extracted/157_qa.md`：F01~F04 四类流程、**用户可见九态 ↔ 内部状态映射**、
  **17 条 `UX-Fxx-xx` 端到端验收用例**、主动任务资源隔离七规则。
- 专项 #75~#97 权威编号对照 = `chat/extracted/专项77-89目录_2026-10-25.md`。
- 语料原始抓取正文可恢复性取证脚本 = `.planning/2026-10-25-product-baseline/recover.py`（顶层是
  `messages` 数组，不是 `conversations[].mapping`）。
- 目标形态与路线总纲 = `docs/产品目标形态与实施路线_2026-10-25.md`；新包规划 **ALN-23~29**
  （ALN-23 入口/模式/工作区、ALN-24 成果与信任、ALN-25 主动帮助、ALN-26 能力缺口编排、
  ALN-27 经验迁移、ALN-28 事实核验(默认不立项)、ALN-29 首发门禁与用例）。
- 规矩：**每张新卡挂一个 UX 编号，出口证据引用 `UX-*` 验收条件或 `UX-Fxx-xx` 用例编号**；
  不臆造专项编号（EA-PRODUCT-007 §十二明令）。

## 待修复（2026-10-23 复核）
- F-1..F-7 已于 2026-10-23 由 claude-main 直接修复（补跑日志、更正计数、台账 next_step、worktree 清理、
  乱码与记忆索引）；逐项处理结果见
  `docs/协作开发/验收/2026-10-23-WorkBuddy工作复核与待修复项.md` 各条的「处理结果」行。
  F-4 采用方案（a）：保留 claude-main 的 accepted 结论并留档裁决原因。
