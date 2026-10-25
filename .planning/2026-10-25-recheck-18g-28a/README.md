ALN-18-G/28-A 固定提交 a1b3a31 独立复验证据（zcode-main，2026-10-25）。

probe_18g.py / probe_28a.py 为独立编写的探针；*_run*.txt、delivery-tests.txt、collab-base.txt 为运行日志。
重跑方式：git worktree add --detach wt a1b3a3183b057b4e72a50677a703555a05ba4acf 后，在本目录执行 python probe_18g.py / python probe_28a.py（wt/ 不入库）。
