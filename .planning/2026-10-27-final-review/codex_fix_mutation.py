"""终审独立复核：对 codex-runtime 自写修复做变异测试。

方法（沿用第三方复核「修复前/后对照」纪律）：
  1. 先在修复后代码上跑其自带测试 → 必须全绿（证明测试本身可运行）；
  2. 逐个把修复回退到修复前的错误实现 → 其自带测试必须失败
     （若仍通过，说明该修复缺乏判别力，不能算被验证）。

每个变异体记录：id / 目标 / 回退方式 / 期望失败 / 实际结果。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
LOG = Path(__file__).resolve().parent / "codex-fix-mutation-results.log"

# codex 自带的相关测试模块（AS-01~AS-05 的相关套件）
TEST_FILE = "tests/test_assigned_scheduler_review_20261026.py"
TEST_FILE2 = "tests/test_new_host_review_20261026.py"
TEST_FILE3 = "tests/test_goal_scheduler.py"
TEST_FILE4 = "tests/test_goal_scheduler_host.py"

SCHED = "evoagent/goals/scheduler.py"
API = "evoagent/api/goals_api.py"
APP = "evoagent/app.py"

# 每个变异体：(id, 目标文件, 修复后文本, 回退文本, 期望)
MUTATIONS = [
    (
        "V1-AS01-恢复500行窗口截断",
        SCHED,
        '            rows = self.db._conn.execute(\n'
        '                f"SELECT data FROM tasks WHERE status IS NULL OR status NOT IN ({placeholders})"\n'
        '                " ORDER BY created_at DESC", terminal).fetchall()',
        '            rows = self.db._conn.execute(\n'
        '                "SELECT data FROM tasks ORDER BY created_at DESC LIMIT 500"\n'
        '            ).fetchall()',
        "AS-01：primary_load 不得只读最近 500 条（终态记录会遮住旧 RUNNING 任务）",
    ),
    (
        "V2-AS02-恢复sync的1000目标窗口",
        SCHED,
        '            goals = conn.execute(\n'
        '                f"SELECT goal_id FROM goals WHERE status NOT IN ({placeholders}) ORDER BY goal_id",\n'
        '                terminal).fetchall()',
        '            goals = [g for g in self.goals.list(limit=1000)\n'
        '                     if g["status"] not in TERMINAL]',
        "AS-02：sync 必须扫描完整非终态目标集合，不得用 UI 列表窗口",
    ),
    (
        "V3-AS03-放开SchedulerPolicy的有限正数校验",
        SCHED,
        "    def __post_init__(self):\n"
        "        for key, value in asdict(self).items():\n"
        "            try:\n"
        "                valid = type(value) in (int, float) and math.isfinite(value) and value > 0\n"
        "            except OverflowError:\n"
        "                valid = False\n"
        "            if not valid:\n"
        "                raise ValueError(key + \" must be a positive finite number\")",
        "    def __post_init__(self):\n"
        "        pass",
        "AS-03：SchedulerPolicy 必须拒绝 Inf/布尔/非正数",
    ),
    (
        "V4-AS04-API恢复float(now)布尔旁路",
        API,
        "        now=now)})",
        "        now=float(now) if now is not None else None)})",
        "AS-04：API 不得把布尔/坏字符串静默转成时间",
    ),
    (
        "V5-AS05-移除调度器的主动准入检查",
        SCHED,
        "        if self.proactivity is not None:\n"
        "            admission = self.proactivity.may_start(\n"
        "                concurrent_proactive=load[\"derived_in_flight\"])\n"
        "            if not admission.allowed:\n"
        "                report[\"reason\"] = \"proactivity_guard\"\n"
        "                report[\"admission\"] = admission.to_dict()\n"
        "                self._emit(\"goal_schedule_yielded\", {\"at\": moment, **report[\"admission\"]})\n"
        "                return self._record_report(report, moment)",
        "        if False:\n"
        "            pass",
        "AS-05：调度派发前必须消费 may_start 主动准入（关闭/高压不得派发）",
    ),
    (
        "V6-AS05-移除App的proactivity注入",
        APP,
        '            owner_id="host_goal_scheduler", proactivity=self.proactivity)',
        '            owner_id="host_goal_scheduler")',
        "AS-05：App 必须把同一个 ProactivityGovernor 注入 GoalScheduler",
    ),
]


def run_tests() -> int:
    """跑 codex 自带的相关测试，返回 pytest 退出码。"""
    cmd = [
        PY, "-m", "pytest",
        TEST_FILE, TEST_FILE2, TEST_FILE3, TEST_FILE4,
        "-q", "--no-header", "--tb=no", "-p", "no:cacheprovider",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    return proc.returncode


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    lines = []

    def emit(text: str) -> None:
        print(text, flush=True)
        lines.append(text)

    emit("=" * 70)
    emit(f"终审独立复核：codex 自写修复的变异判别力检验")
    emit(f"ROOT={ROOT}")
    emit(f"起点={datetime.now().isoformat(timespec='seconds')}")
    emit("=" * 70)

    # ---- 基线：修复后必须全绿 ----
    base = run_tests()
    emit(f"\n[基线] 修复后跑 codex 自带相关测试 → exit={base} "
         f"（期望 0）")
    if base != 0:
        emit("!! 基线非绿，无法进行变异判别力检验，终止")
        LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 2

    results = []
    for mid, target, fixed, reverted, why in MUTATIONS:
        path = ROOT / target
        original = path.read_text(encoding="utf-8")
        if fixed not in original:
            emit(f"\n[{mid}] !! 锚点未命中（代码已漂移），跳过")
            results.append({"id": mid, "target": target, "result": "anchor_missed",
                            "expected": "fail", "why": why})
            continue
        backup = original
        try:
            write(path, original.replace(fixed, reverted, 1))
            code = run_tests()
            caught = (code != 0)
            emit(f"\n[{mid}] 回退修复 → 跑其自带测试 exit={code} → "
                 f"{'捕获 ✓' if caught else '存活 ✗（无判别力）'}")
            emit(f"    目标: {target}")
            emit(f"    理由: {why}")
            results.append({"id": mid, "target": target, "exit_code": code,
                            "result": "caught" if caught else "survived",
                            "expected": "fail", "why": why})
        finally:
            write(path, backup)

    # ---- 复原校验：确保所有文件回到基线 ----
    after = run_tests()
    emit(f"\n[复原校验] 全部变异回退后重跑 → exit={after} （期望 0）")
    emit("=" * 70)
    caught_n = sum(1 for r in results if r["result"] == "caught")
    survived_n = sum(1 for r in results if r["result"] == "survived")
    missed_n = sum(1 for r in results if r["result"] == "anchor_missed")
    emit(f"结论：捕获 {caught_n} / 存活 {survived_n} / 锚点未命中 {missed_n} "
         f"（共 {len(results)} 个变异体）")
    emit("存活含义：codex 的该修复缺乏测试判别力 → 该项不可视为已验证")
    emit("=" * 70)

    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (LOG.parent / "codex-fix-mutation-results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if survived_n == 0 and missed_n == 0 and after == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
