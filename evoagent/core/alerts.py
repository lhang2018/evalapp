"""分级告警规则（PRD 40 / Arch 20.2）。

告警基于聚合指标，不基于单条原始日志；评估是无状态的，重复调用幂等。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AlertPolicy:
    min_success_rate: float = 0.80
    min_terminal_tasks: int = 5
    max_consecutive_failures: int = 5
    min_model_success_rate: float = 0.90
    min_model_calls: int = 5
    max_contract_error_rate: float = 0.05
    min_tool_calls: int = 10
    max_daily_cost_ratio: float = 0.90
    max_rollback_rate: float = 0.30
    min_deployments: int = 1
    max_react_anomaly_rate: float = 0.20
    min_react_runs: int = 5


def evaluate_alerts(snapshot: Dict[str, Any],
                    policy: Optional[AlertPolicy] = None,
                    daily_cost_limit: Optional[float] = None) -> List[Dict[str, Any]]:
    policy = policy or AlertPolicy()
    alerts: List[Dict[str, Any]] = []

    def add(rule: str, severity: str, message: str,
            value: Any, threshold: Any) -> None:
        alerts.append({"rule": rule, "severity": severity,
                       "message": message, "value": value,
                       "threshold": threshold})

    tasks = snapshot.get("tasks") or {}
    terminal = tasks.get("terminal") or 0
    success_rate = tasks.get("success_rate")
    if terminal >= policy.min_terminal_tasks and success_rate is not None \
            and success_rate < policy.min_success_rate:
        add("task_success_rate",
            "critical" if success_rate < 0.5 else "warning",
            "任务成功率低于阈值",
            success_rate, policy.min_success_rate)

    consecutive = tasks.get("consecutive_failures") or 0
    if consecutive >= policy.max_consecutive_failures:
        add("consecutive_failures", "warning",
            "连续失败任务数达到阈值", consecutive,
            policy.max_consecutive_failures)

    models = snapshot.get("model_calls") or {}
    if (models.get("total") or 0) >= policy.min_model_calls \
            and models.get("success_rate") is not None \
            and models["success_rate"] < policy.min_model_success_rate:
        add("model_call_success_rate", "warning",
            "模型调用成功率低于阈值", models["success_rate"],
            policy.min_model_success_rate)

    tools = snapshot.get("tools") or {}
    if (tools.get("calls") or 0) >= policy.min_tool_calls \
            and tools.get("contract_error_rate") is not None \
            and tools["contract_error_rate"] > policy.max_contract_error_rate:
        add("tool_contract_error_rate", "warning",
            "工具契约错误率高于阈值", tools["contract_error_rate"],
            policy.max_contract_error_rate)

    if daily_cost_limit and daily_cost_limit > 0:
        cost = float((snapshot.get("budget") or {}).get(
            "runtime_day", {}).get("cost", 0.0))
        ratio = cost / daily_cost_limit
        if ratio >= policy.max_daily_cost_ratio:
            add("daily_cost_ratio",
                "critical" if ratio >= 1.0 else "warning",
                "运行日成本接近或超过限额", round(ratio, 4),
                policy.max_daily_cost_ratio)

    releases = snapshot.get("releases") or {}
    deployments = releases.get("deployments") or 0
    rollbacks = releases.get("rollbacks") or 0
    if deployments >= policy.min_deployments:
        rate = rollbacks / deployments
        if rate > policy.max_rollback_rate:
            add("release_rollback_rate", "warning",
                "发布后回滚率高于阈值", round(rate, 4),
                policy.max_rollback_rate)

    react = snapshot.get("react") or {}
    if (react.get("runs") or 0) >= policy.min_react_runs \
            and react.get("anomaly_rate") is not None \
            and react["anomaly_rate"] > policy.max_react_anomaly_rate:
        add("react_anomaly_rate", "warning",
            "ReAct 循环异常率高于阈值", react["anomaly_rate"],
            policy.max_react_anomaly_rate)

    drift = snapshot.get("drift") or {}
    if drift.get("status") == "drifting":
        signals = drift.get("signals") or []
        critical = any(s.get("severity") == "critical" for s in signals)
        kinds = sorted({str(s.get("kind")) for s in signals})
        add("environment_drift", "critical" if critical else "warning",
            "检测到环境漂移: " + ", ".join(kinds),
            drift.get("signal_count"), 0)

    security = (snapshot.get("events") or {}).get("security_incidents") or 0
    if security > 0:
        add("security_incident", "critical",
            "检测到安全相关严重事件", security, 0)

    return alerts
