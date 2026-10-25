"""执行模式路由（ALN-08-W）。

主链路由（``mode_router``）只读消费 ALN-07-W 的匹配器，不重复实现五项检查，
也不改 ``planning/`` 已接受切片。
"""

from .mode_router import (DYNAMIC, HYBRID, MODES, STABLE, ModeRouter,
                          candidate_from_workflow)

__all__ = ["DYNAMIC", "HYBRID", "MODES", "STABLE", "ModeRouter",
           "candidate_from_workflow"]
