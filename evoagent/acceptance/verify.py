"""结果验收（PRD 4.1 / 15.4.8 / Architecture 5.2 验收执行器）。

原则：执行组件不能仅凭自身报告宣布成功；
模型自评不等于客观真值；计划完成 ≠ 实际完成。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..tools.contract import ToolResult


@dataclass
class Verdict:
    passed: bool
    checks: List[Dict[str, Any]] = field(default_factory=list)
    uncertainty: bool = False          # 无法完全自动判定的任务应标记评测不确定性

    def summary(self) -> str:
        ok = sum(1 for c in self.checks if c.get("passed"))
        return f"{ok}/{len(self.checks)} 通过"


class Verifier:
    """规则验收器：按任务验收条件逐项检查最终结果与运行证据。"""

    def check(self, criteria: List[Dict[str, Any]],
              answer: Optional[str],
              tool_results: List[ToolResult]) -> Verdict:
        checks: List[Dict[str, Any]] = []
        # 未定义验收条件时使用默认要求：结果非空（PRD 7.4 / 独立验收基线）
        if not criteria:
            criteria = [{"type": "result_nonempty"}]
        for c in criteria or []:
            ctype = c.get("type", "result_nonempty")
            if ctype == "result_nonempty":
                passed = bool(answer and answer.strip())
            elif ctype == "result_contains":
                passed = bool(answer) and (c["value"] in answer)
            elif ctype == "result_matches":
                passed = bool(answer) and bool(re.search(c["pattern"], answer))
            elif ctype == "tool_succeeded":
                tool = c.get("tool")
                matched = [r for r in tool_results
                           if r.ok and (tool is None
                                        or r.evidence.get("tool") == tool)]
                passed = bool(matched)
            elif ctype == "tool_goal_reached":
                passed = any(r.goal_reached for r in tool_results)
            elif ctype == "data_equals":
                passed = any(
                    str(r.data) == str(c["value"]) for r in tool_results)
            elif ctype == "custom":
                fn: Optional[Callable[..., bool]] = c.get("fn")
                try:
                    passed = bool(fn(answer, tool_results)) if fn else False
                except Exception:
                    passed = False
                    # 校验器自身故障 → 标记不确定，不算失败也不算通过
                    checks.append({"type": ctype, "passed": False,
                                   "uncertain": True,
                                   "error": "custom verifier raised"})
                    continue
            else:
                passed = False
            checks.append({"type": ctype, "spec": c, "passed": bool(passed)})
        return Verdict(passed=all(c["passed"] for c in checks) if checks else False,
                       checks=checks)

    def post_release_review(self) -> bool:
        """主观创作质量指标允许人工验收与用户修改（PRD 78.7）。默认交给人工。"""
        return False
