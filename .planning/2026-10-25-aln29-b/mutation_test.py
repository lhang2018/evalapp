"""变异测试：证明 tests/test_release_gates.py 对校验器有判别力。

做法：把脚本复制到隔离目录，分别注入三处缺陷，期望对应测试从通过变为失败。
不碰主工作区，不改仓库文件。
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "check_release_gates.py"
TEST = ROOT / "tests" / "test_release_gates.py"
EVIDENCE = ROOT / "docs/协作开发/验收/首发门禁证据_2026-10-25.json"
BOARD = ROOT / "docs/协作开发/workboard.json"
PY = sys.executable

MUTANTS = [
    ("M1 去掉 pass 必须有证据的检查",
     "        if not evidence:",
     "        if False and not evidence:",
     "test_pass_without_evidence_is_rejected"),
    ("M2 未测试不再阻止发布",
     '"release_ready": not failed and not untested and not range_missing and not blocking,',
     '"release_ready": not failed and not range_missing and not blocking,',
     "test_untested_gate_is_not_counted_as_passed"),
    ("M3 去掉 G6 与台账 published_commit 的交叉校验",
     '        if gate.get("id") == "G6" and gate.get("status") == "pass" and not published:',
     '        if False:',
     "test_g6_pass_without_published_commit_is_rejected"),
    ("M4 去掉 untested 挂证据的自洽检查",
     "        if evidence:",
     "        if False:",
     "test_untested_with_evidence_is_rejected"),
    ("M5 门禁编号校验改为只数数量",
     "    if gate_ids != GATE_IDS:",
     "    if len(gate_ids) != 8:",
     "test_renumbered_gate_is_rejected"),
    ("M6 范围项编号校验改为只数数量",
     "    if range_ids != RANGE_IDS:",
     "    if len(range_ids) != 13:",
     "test_renumbered_range_item_is_rejected"),
]


def build(tmp, script_text):
    (tmp / "tools").mkdir(parents=True, exist_ok=True)
    (tmp / "tests").mkdir(parents=True, exist_ok=True)
    (tmp / "docs/协作开发/验收").mkdir(parents=True, exist_ok=True)
    (tmp / "docs/协作开发").mkdir(parents=True, exist_ok=True)
    (tmp / "tools/check_release_gates.py").write_text(script_text, encoding="utf-8")
    shutil.copy(TEST, tmp / "tests/test_release_gates.py")
    shutil.copy(EVIDENCE, tmp / "docs/协作开发/验收/首发门禁证据_2026-10-25.json")
    shutil.copy(BOARD, tmp / "docs/协作开发/workboard.json")


def run(tmp, extra=None):
    cmd = [PY, "-m", "pytest", "tests/test_release_gates.py", "-q"]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=str(tmp))


results = []
base = SCRIPT.read_text(encoding="utf-8")
with tempfile.TemporaryDirectory(prefix="aln29b-mut-") as raw:
    base_dir = Path(raw)
    build(base_dir, base)
    baseline = run(base_dir)
    results.append(("基线（未变异）", baseline.returncode == 0, "全部通过", ""))
    for name, old, new, expect_fail in MUTANTS:
        if old not in base:
            results.append((name, False, "未找到变异锚点", expect_fail))
            continue
        mutated = base.replace(old, new, 1)
        with tempfile.TemporaryDirectory(prefix="aln29b-mut-") as raw2:
            mut_dir = Path(raw2)
            build(mut_dir, mutated)
            outcome = run(mut_dir)
            combined = outcome.stdout + outcome.stderr
            caught = expect_fail in combined and outcome.returncode != 0
            results.append((name, caught, f"退出码 {outcome.returncode}",
                            expect_fail if caught else f"未触发 {expect_fail}"))
print("=" * 78)
print("变异测试：证明校验器测试对注入缺陷有判别力")
print("=" * 78)
for name, ok, detail, extra in results:
    flag = "✓" if ok else "✗"
    print(f"{flag} {name}：{detail} {extra}")
passed = sum(1 for r in results if r[1])
print("-" * 78)
print(f"{passed}/{len(results)} 项变异被测试捕获")
sys.exit(0 if passed == len(results) else 1)
