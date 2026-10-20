"""内置评测集（PRD 51-53 / Architecture 11）。

分层：单元/契约、集成、能力、端到端。内置集只覆盖可确定性重放的端到端用例，
业务评测集应由宿主按领域注入，避免调优数据与验收数据混用。
"""

from typing import Dict

from .evaluator import EvalCase, EvalDataset


def builtin_datasets() -> Dict[str, EvalDataset]:
    smoke = EvalDataset(
        dataset_id="builtin_smoke",
        version="1.0.0",
        task_category="general",
        source="builtin",
        cases=[
            EvalCase(case_id="smoke_time", input_goal="现在几点了",
                     intent="time_query", domain="general",
                     expected=[{"type": "tool_succeeded"}], difficulty="easy"),
            EvalCase(case_id="smoke_calc", input_goal="计算 2024 + 2000 + 500",
                     intent="computation", domain="general",
                     expected=[{"type": "tool_goal_reached"}],
                     difficulty="easy"),
            EvalCase(case_id="smoke_time_2", input_goal="告诉我当前时间",
                     intent="time_query", domain="general",
                     expected=[{"type": "result_nonempty"}],
                     difficulty="easy"),
        ])
    holdout = EvalDataset(
        dataset_id="builtin_holdout",
        version="1.0.0",
        task_category="general",
        source="builtin_holdout",
        cases=[
            EvalCase(case_id="holdout_calc", input_goal="计算 7 * 8",
                     intent="computation", domain="general",
                     expected=[{"type": "tool_goal_reached"}],
                     difficulty="easy", holdout=True),
        ])
    return {smoke.dataset_id: smoke, holdout.dataset_id: holdout}
