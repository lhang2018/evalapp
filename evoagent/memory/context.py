"""上下文装配与治理（PRD 29-33 / Architecture 8.5-8.7）。

- 装配优先级（Arch 8.5）：安全约束 > 目标 > 硬约束 > 状态 > 工具说明 > 记忆 > 执行结果 > 验收条件 > 证据。
- 压缩必须同时维护摘要与结构化状态，压缩后做关键约束丢失检查。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..models.base import ChatMessage
from .store import Credibility, MemoryKind, MemoryStore


@dataclass
class ContextBudget:
    max_chars: int = 6000


@dataclass
class AssembledContext:
    # 结构化状态（不因摘要丢失 —— Arch 8.6）
    task_goal: str = ""
    hard_constraints: List[str] = field(default_factory=list)
    task_status: str = ""
    acceptance_criteria: List[Dict] = field(default_factory=list)
    tools: List[Dict] = field(default_factory=dict)  # type: ignore[arg-type]
    recent_results: List[str] = field(default_factory=list)
    memories: List[str] = field(default_factory=list)
    domain: str = "general"
    intent: str = "general"
    # 记忆使用统计（PRD 25 / 32）
    memory_candidates: int = 0
    memory_used: int = 0
    # 压缩检查
    chars_before: int = 0
    chars_after: int = 0
    dropped_memories: int = 0
    compression_dropped_constraints: List[str] = field(default_factory=list)

    def stats(self) -> Dict[str, int]:
        return {
            "memory_candidates": self.memory_candidates,
            "memory_used": self.memory_used,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "dropped_memories": self.dropped_memories,
        }

    def to_messages(self) -> List[ChatMessage]:
        """装配为模型可见信息视图。"""
        msgs = [ChatMessage(role="system",
                            content=f"intent:{self.intent}|domain:{self.domain}")]
        if self.hard_constraints:
            msgs.append(ChatMessage(role="system",
                        content="硬约束(不可省略): " + " | ".join(self.hard_constraints)))
        msgs.append(ChatMessage(role="user", content=f"goal:{self.task_goal}"))
        if self.memories:
            msgs.append(ChatMessage(role="system",
                        content="相关记忆(候选/已验证): " + " || ".join(self.memories)))
        if self.task_status:
            msgs.append(ChatMessage(role="system",
                        content=f"任务状态: {self.task_status}"))
        if self.recent_results:
            msgs.append(ChatMessage(role="system",
                        content="最近执行结果: " + " | ".join(self.recent_results[-3:])))
        if self.acceptance_criteria:
            msgs.append(ChatMessage(role="system",
                        content=f"验收条件: {self.acceptance_criteria}"))
        return msgs


class ContextAssembler:
    def __init__(self, memory_store: Optional[MemoryStore] = None,
                 budget: Optional[ContextBudget] = None):
        self.memory = memory_store
        self.budget = budget or ContextBudget()

    def assemble(self, goal: str, *, intent: str = "general",
                 domain: str = "general", constraints: List[str] | None = None,
                 status: str = "", acceptance: List[Dict] | None = None,
                 recent_results: List[str] | None = None,
                 scope_key: str = "") -> AssembledContext:
        ctx = AssembledContext(
            task_goal=goal,
            hard_constraints=list(constraints or []),
            task_status=status,
            acceptance_criteria=list(acceptance or []),
            intent=intent, domain=domain,
            recent_results=list(recent_results or []),
        )
        # 记忆检索（进入上下文预算内）
        if self.memory is not None:
            mems = self.memory.search(goal, domains=[domain], scope_key=scope_key)
            ctx.memory_candidates = len(mems)
            for m in mems:
                ctx.memories.append(f"[{m.status.value}] {m.content}")
                ctx.memory_used += 1
                self.memory.record_use(m.memory_id)
        # 上下文超限压缩（保留结构化字段，只截断自由文本记忆/结果）
        ctx.chars_before = _ctx_size(ctx)
        self._apply_budget(ctx)
        ctx.memory_used = len(ctx.memories)
        ctx.chars_after = _ctx_size(ctx)
        return ctx

    def _apply_budget(self, ctx: AssembledContext) -> None:
        text = _ctx_size(ctx)
        while text > self.budget.max_chars and ctx.memories:
            ctx.memories.pop(0)   # 从最旧记忆开始压缩
            ctx.dropped_memories += 1
            text = _ctx_size(ctx)
        # 约束永远保留（Arch 8.5）
        if text > self.budget.max_chars and ctx.recent_results:
            drop_n = max(1, len(ctx.recent_results) // 2)
            ctx.recent_results = ctx.recent_results[drop_n:]


def _ctx_size(ctx: AssembledContext) -> int:
    return len(" ".join(m.content for m in ctx.to_messages()))


def check_compression(before: AssembledContext, after: AssembledContext) -> List[str]:
    """压缩后检查（PRD 31）：关键约束/状态/验收/待办是否丢失。"""
    dropped = []
    for c in before.hard_constraints:
        if c not in after.hard_constraints:
            dropped.append(f"约束丢失: {c}")
    if before.task_goal and not after.task_goal:
        dropped.append("目标丢失")
    if before.acceptance_criteria and not after.acceptance_criteria:
        dropped.append("验收条件丢失")
    return dropped
