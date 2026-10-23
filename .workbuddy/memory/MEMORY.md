# evalapp 项目长期备忘（跨会话）

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
