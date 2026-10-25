"""ALN-25-A 变异测试：证明 tests/test_proactive_assist.py 对核心断言有判别力。

用法：python .planning/2026-10-25-aln25a/mutation_test.py [target_dir]
- target_dir 为含本卡改动的目录（默认隔离 worktree 的 Windows 路径）；
- 先在原文件上跑基线（必须全绿），再逐个施加变异体，要求每个变异体都被
  测试套件捕获（pytest 退出码非 0），最后恢复原文件并复跑基线；
- 结果写入 mutation-results.log（同目录），数字一律以日志为准。

变异体覆盖卡验收三条出口：
1. 有来源/事实推断未知区分（M1-M6：范围门禁、四级分类、过时/冲突/过期）；
2. 无高价值机会可不建议（M7/M8/M12：门禁、needs_evidence、withheld）；
3. 四段式建议与不自动执行（M9-M11：范围外排除、权限解析、授权字段）。
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PY = r"C:/Users/lhang/AppData/Local/Programs/Python/Python310/python.exe"
DEFAULT_TARGET = r"C:\Users\lhang\AppData\Local\Temp\workbuddy-update-x64\aln25a-iso"
TEST = "tests/test_proactive_assist.py"

MUTATIONS = [
    ("M1", "记忆无策略服务时 fail-open（应为 fail-closed）",
     '                memory_denied.append({"scope": scope, "reason": "no_scope_policy_service"})',
     '                memory_allowed.append(scope)'),
    ("M2", "忽略 24-D 作用域允许策略（deny 失效）",
     '            elif not self.memory_control.scope_allowed(scope):',
     '            elif False:'),
    ("M3", "用户明说降级为 recorded（四级分类失真）",
     '                    add("confirmed_constraints", ref=ref, summary=summary, evidence="user_stated",',
     '                    add("confirmed_constraints", ref=ref, summary=summary, evidence="recorded",'),
    ("M4", "过时任务状态当作当前事实（stale 判定失效）",
     '        if age > STALE_TASK_SECONDS:',
     '        if False:'),
    ("M5", "冲突记忆不标记待确认",
     '                if item.has_conflict:',
     '                if False:'),
    ("M6", "过期记忆不标记待确认",
     '                elif item.is_expired(now):',
     '                elif False:'),
    ("M7", "移除高价值效用门槛",
     '            elif candidate["score"] <= min_score:',
     '            elif False:'),
    ("M8", "needs_evidence 候选混入建议",
     '            elif state != "ranked":',
     '            elif False:'),
    ("M9", "范围外机会候选不再排除",
     '            if candidate["source_ref"] not in authorized:',
     '            if False:'),
    ("M10", "授权字段篡改为已执行（违反建议≠授权）",
     '            "authorization": {"executed": False, "auto_executed": False,',
     '            "authorization": {"executed": True, "auto_executed": False,'),
    ("M11", "无授权 Grants 的来源谎称权限已解析",
     '''        if policy is None:
            return {"resolved": False, "goal_id": goal_id,
                    "note": "来源目标无有效授权 Grants；执行前须显式授权"}''',
     '''        if policy is None:
            return {"resolved": True, "goal_id": goal_id, "tools": [],
                    "permissions": [], "max_risk": "critical"}'''),
    ("M12", "无高价值时不返回 withheld 原因",
     '                "withheld": None if suggestions else "no_high_value_opportunity",',
     '                "withheld": None,'),
]


def run_tests(target):
    proc = subprocess.run([PY, "-m", "pytest", TEST, "-q", "--no-header", "--tb=no"],
                          cwd=target, capture_output=True, text=True, timeout=900)
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    return proc.returncode, lines[-1] if lines else "(no output)"


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT_TARGET)
    assist = target / "evoagent" / "goals" / "assist.py"
    original = assist.read_text(encoding="utf-8")
    log_path = target.parent / "mutation-results.log"
    report = []
    caught = survived = 0

    def say(line):
        print(line)
        report.append(line)

    say("ALN-25-A 变异测试 %s" % datetime.now().isoformat(timespec="seconds"))
    say("target=%s" % target)
    code, tail = run_tests(target)
    say("[baseline] exit=%d %s" % (code, tail))
    if code != 0:
        say("基线未过，终止（变异判别力无从谈起）")
        log_path.write_text("\n".join(report) + "\n", encoding="utf-8")
        return 1

    for mid, desc, old, new in MUTATIONS:
        if old not in original:
            say("[%s] ANCHOR-MISS %s" % (mid, desc))
            survived += 1
            continue
        assist.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, tail = run_tests(target)
        finally:
            assist.write_text(original, encoding="utf-8")
        if code != 0:
            caught += 1
            say("[%s] CAUGHT   %s -> %s" % (mid, desc, tail))
        else:
            survived += 1
            say("[%s] SURVIVED %s" % (mid, desc))

    code, tail = run_tests(target)
    say("[restored-baseline] exit=%d %s" % (code, tail))
    say("summary: caught=%d survived=%d total=%d" % (caught, survived, len(MUTATIONS)))
    log_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("log -> %s" % log_path)
    return 0 if survived == 0 and code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
