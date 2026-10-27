"""07-C 一审独立静态确认：核对 RG-01/02/03 与裁决持久化的修复点存在。"""
import ast
import pathlib

root = pathlib.Path(".")
controller = (root / "evoagent/core/controller.py").read_text(encoding="utf-8")
machine = (root / "evoagent/core/state_machine.py").read_text(encoding="utf-8")

checks = {
    "RG-01 只读工具不作为副作用声明": (
        'contract.side_effect_level == "none"' in controller
        and "step.tool_hint = None" in controller
    ),
    "RG-02 门禁活动登记受清理保护": (
        "_unregister_active" in controller
        and "try:" in controller
        and "finally:" in controller
    ),
    "RG-03 SUSPENDED→RECOVERING 合法边": (
        "TaskStatus.SUSPENDED" in machine and "RECOVERING" in machine
    ),
    "裁决先随状态提交再发事件(33c22a1)": (
        "Commit the question with its state before transition events" in controller
    ),
}

# 结构化确认：state_machine 的允许转移表里确实含 SUSPENDED->RECOVERING
tree = ast.parse(machine)
edge = False
for node in ast.walk(tree):
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Attribute) and key.attr == "SUSPENDED"):
                continue
            sets = []
            if isinstance(value, ast.Set):
                sets = [value]
            elif isinstance(value, ast.Call):
                sets = [arg for arg in value.args if isinstance(arg, ast.Set)]
            for group in sets:
                targets = [e.attr for e in group.elts
                           if isinstance(e, ast.Attribute)]
                if "RECOVERING" in targets:
                    edge = True
checks["SUSPENDED 转移表含 RECOVERING"] = edge

for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
print("ALL_PASS" if all(checks.values()) else "HAS_FAILURE")
