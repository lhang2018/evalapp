"""ALN-21-A 变异测试：证明 Windows 终端验证套件的判别力。

在隔离 worktree 内运行：对 6 个核心断言点逐一变异，要求全部被
tests/test_windows_terminal_validation.py 捕获（测试失败即捕获）。
先跑未变异基线证明全绿，再逐个变异。

用法（cwd = 隔离 worktree 根）：
    python .planning/2026-10-25-aln21a/mutation_test.py
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
PY = sys.executable
TESTS = "tests/test_windows_terminal_validation.py"

RETIRE_GUARD_OLD = (
    "        if action in TRUST_BOUND_ACTIONS and hasattr(\n"
    "                self.releases, \"is_retired\") \\\n"
    "                and self.releases.is_retired(resolved_release):")

MUTANTS = [
    ("M1", "evoagent/release/delivery.py",
     'PLATFORMS = ("android", "ios", "windows")',
     'PLATFORMS = ("android", "ios")',
     "平台门回退：Windows 终端不可对账"),
    ("M2", "evoagent/release/manager.py",
     '_SUPPORTED_PLATFORMS = ("android", "ios", "windows")',
     '_SUPPORTED_PLATFORMS = ("android", "ios")',
     "Windows 制品清单校验失败"),
    ("M3", "evoagent/release/delivery.py",
     RETIRE_GUARD_OLD,
     "        if False:",
     "退役版本可被激活/安装"),
    ("M4", "evoagent/release/delivery.py",
     "            if signing_key_id and signing_key_id != server_key_id:",
     "            if False:",
     "自报密钥与清单不一致也放行"),
    ("M5", "evoagent/deployment/bootstrap.py",
     'return {"ok": not errors, "errors": errors, "manifest": manifest,',
     'return {"ok": True, "errors": errors, "manifest": manifest,',
     "部署校验失去 fail-closed"),
    ("M6", "evoagent/recovery/combined.py",
     '        if not plan["compatible"]:',
     "        if False:",
     "不兼容备份不再拒绝"),
]


def run_tests():
    proc = subprocess.run(
        [PY, "-m", "pytest", TESTS, "-q", "--no-header", "--tb=no"],
        capture_output=True, text=True, timeout=600, cwd=str(ROOT))
    tail = [line for line in proc.stdout.splitlines()
            if ("passed" in line or "failed" in line or "error" in line)
            and "safe-delete" not in line]
    summary = tail[-1].strip() if tail else f"exit={proc.returncode}"
    return summary, proc.returncode


def main() -> int:
    lines = []
    base_summary, base_code = run_tests()
    lines.append(f"基线（未变异）: {base_summary}")
    print(lines[-1])
    if base_code != 0:
        lines.append("基线非全绿，终止变异测试")
        Path("mutation-results.log").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        return 1

    caught = 0
    for mid, relpath, old, new, desc in MUTANTS:
        path = ROOT / relpath
        original = path.read_text(encoding="utf-8")
        if old not in original:
            lines.append(f"{mid} 靶点未找到（{relpath}），跳过")
            print(lines[-1])
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            summary, code = run_tests()
        finally:
            path.write_text(original, encoding="utf-8")
        ok = code != 0
        caught += 1 if ok else 0
        lines.append(f"{mid} [{'捕获' if ok else '存活!!'}] {desc}"
                     f" -> {summary}")
        print(lines[-1])

    lines.append(f"总计: {caught}/{len(MUTANTS)} 捕获"
                 + ("" if caught == len(MUTANTS) else "（有存活，须补测试）"))
    print(lines[-1])
    Path("mutation-results.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return 0 if caught == len(MUTANTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
