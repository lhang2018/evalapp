"""统一任务状态机（Architecture 3.3）。

终态不能被普通动作随意改写；重新执行应创建新的运行记录。
所有状态转换必须经由 transition() 校验。
"""

from typing import FrozenSet, Tuple

from .task import TaskStatus

ALLOWED_TRANSITIONS: dict[TaskStatus, FrozenSet[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.CLASSIFYING, TaskStatus.CANCELLED}),
    TaskStatus.CLASSIFYING: frozenset({TaskStatus.PLANNING, TaskStatus.READY, TaskStatus.WAITING_USER, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.PLANNING: frozenset({TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.WAITING_USER, TaskStatus.WAITING_DEPENDENCY,
        TaskStatus.RECOVERING, TaskStatus.VERIFYING, TaskStatus.SUSPENDED, TaskStatus.CANCELLED,
    }),
    TaskStatus.RECOVERING: frozenset({TaskStatus.RUNNING, TaskStatus.SUSPENDED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.WAITING_USER: frozenset({TaskStatus.RUNNING, TaskStatus.SUSPENDED,
                                      TaskStatus.CANCELLED}),
    TaskStatus.WAITING_DEPENDENCY: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.VERIFYING: frozenset({TaskStatus.SUCCEEDED, TaskStatus.RECOVERING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.SUSPENDED: frozenset({TaskStatus.RUNNING, TaskStatus.CLASSIFYING,
                                   TaskStatus.RECOVERING,
                                   TaskStatus.CANCELLED}),
    # 终态：无出边（该规则由下面的 can_transition 强制）
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class IllegalTransition(Exception):
    pass


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def transition(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    """校验并返回目标状态；非法转换抛错。"""
    if not can_transition(current, target):
        raise IllegalTransition(f"非法状态转换: {current.value} -> {target.value}")
    return target
