"""Compensation uses frozen action arguments and independent restoration proof."""
from copy import deepcopy


def resolve(value, bindings):
    if isinstance(value, str) and value.startswith("$"):
        parts = value[1:].split(".")
        current = bindings
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                raise ValueError("补偿引用无法解析: " + value)
            current = current[part]
        return deepcopy(current)
    if isinstance(value, dict):
        return {key: resolve(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, bindings) for item in value]
    return deepcopy(value)


def compensate(tools, wf, done, ctx_input, execution_context, budget):
    outcomes = []
    ordered, seen = [], set()
    def visit(node):
        if node.node_id in seen:
            return
        seen.add(node.node_id)
        for identity in node.deps:
            dependency = wf.node(identity)
            if dependency:
                visit(dependency)
        ordered.append(node)
    for node in wf.nodes:
        visit(node)
    for node in reversed(ordered):
        policy = wf.compensation_actions.get(node.node_id)
        evidence = node.tool_result or {}
        if not policy or not evidence.get("executed"):
            continue
        row = {"node": node.node_id, "verified": False}
        outcomes.append(row)
        try:
            if not isinstance(policy, dict) or not all(policy.get(k) for k in ("tool", "verify_tool")):
                raise ValueError("补偿必须声明工具、参数绑定与独立验证工具")
            effect_id = (evidence.get("evidence") or {}).get("effect_id")
            operation = tools.effects.operation(effect_id) if tools.effects and effect_id else None
            if not operation or operation["status"] not in ("confirmed", "compensated"):
                raise ValueError("缺少可核验的原操作记录")
            if operation["task_id"] != execution_context["task_id"] or operation["tool_id"] != node.tool:
                raise ValueError("补偿证据不属于当前任务节点")
            if not operation["result"]:
                raise ValueError("缺少原操作持久化结果")
            bindings = {"args": operation["args"], "output": operation["result"].get("data"),
                        "ctx": ctx_input, "nodes": done}
            args = resolve(policy.get("args", {}), bindings)
            verify_args = resolve(policy.get("verify_args", {}), bindings)
            verifier = tools.contract(policy["verify_tool"])
            if verifier is None or verifier.side_effect_level != "none":
                raise ValueError("补偿验证必须是独立只读工具")
            context = {"task_id": execution_context["task_id"], "run_id": execution_context["run_id"],
                       "allowed_tools": execution_context.get("allowed_tools"), "budget": budget,
                       "authorization_context": {"scope": "compensation:" + effect_id}}
            if operation["status"] != "compensated":
                restored = tools.execute(policy["tool"], args,
                    idempotency_key="compensate:" + effect_id, **context)
                row["result"] = restored.to_dict()
                if not restored.ok or restored.side_effect_uncertain:
                    row["error"] = restored.error
                    break
            check = tools.execute(policy["verify_tool"], verify_args, **context)
            row["verification"] = check.to_dict()
            if not (check.ok and check.independently_verified and check.goal_reached is True):
                row["error"] = "补偿后外部状态未确认恢复"
                break
            if not tools.effects.transition(effect_id, "compensated",
                    receipt_ref="verified-compensation:" + effect_id, reconciled=True):
                raise ValueError("补偿验证通过但状态未可靠落账")
            row["verified"] = True
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            break
    return outcomes
