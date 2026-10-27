"""终审独立复核（第二批）：codex 复核之后新增的三个修复提交的判别力检验。

对象（均为 2026-10-27 10:25~10:35 产生，晚于 codex 固定源 f973dcc）：
  b2f3b0c  ALN-30-B  全量产物下载端点（api/artifacts_api.py + 客户端 panel）
  b9d9229  ALN-16-Q  证据导出分块（api/evidence_api.py）
  c181bad  ALN-18-H  编码包升级回滚（experts/coding_consumer.py）

方法：回退修复 → 其自带测试必须失败（否则该修复无判别力，不可算已验证）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
LOG = Path(__file__).resolve().parent / "post-codex-fix-mutation-results.log"

ART = "evoagent/api/artifacts_api.py"
EVI = "evoagent/api/evidence_api.py"
COD = "evoagent/experts/coding_consumer.py"

MUTATIONS = [
    (
        "P1-30B-移除下载大小上限拒绝",
        ART,
        "    if size > DOWNLOAD_MAX_BYTES:",
        "    if False:",
        "tests/test_artifacts_api.py",
        "下载超过 32MB 上限必须被拒",
    ),
    (
        "P2-16Q-移除limit上限钳制",
        EVI,
        "    return offset, min(limit, MAX_CHUNK_BYTES), None",
        "    return offset, limit, None",
        "tests/test_evidence_exports_chunk.py",
        "导出 limit 必须被钳制到 MAX_CHUNK_BYTES（防超大导出）",
    ),
    (
        "P3-16Q-移除非法offset/limit拒绝",
        EVI,
        "    if offset < 0 or limit <= 0:",
        "    if False:",
        "tests/test_evidence_exports_chunk.py",
        "负 offset / 非正 limit 必须返回 400",
    ),
]


def run_tests(test_file: str) -> int:
    proc = subprocess.run(
        [PY, "-m", "pytest", test_file, "-q", "--no-header", "--tb=no",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    lines = []

    def emit(text: str) -> None:
        print(text, flush=True)
        lines.append(text)

    emit("=" * 70)
    emit("终审独立复核第二批：codex 之后新增修复的判别力检验")
    emit(f"ROOT={ROOT}")
    emit(f"起点={datetime.now().isoformat(timespec='seconds')}")
    emit("=" * 70)

    # 基线：三个测试文件在修复后必须全绿
    baselines = {}
    for tf in ("tests/test_artifacts_api.py",
               "tests/test_evidence_exports_chunk.py",
               "tests/test_coding_expert_consumer_upgrade.py"):
        code = run_tests(tf)
        baselines[tf] = code
        emit(f"\n[基线] {tf} → exit={code} （期望 0）")
    if any(v != 0 for v in baselines.values()):
        emit("!! 基线非绿，终止")
        LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 2

    results = []
    for mid, target, fixed, reverted, tf, why in MUTATIONS:
        path = ROOT / target
        original = path.read_text(encoding="utf-8")
        if fixed not in original:
            emit(f"\n[{mid}] !! 锚点未命中，跳过")
            results.append({"id": mid, "result": "anchor_missed", "why": why})
            continue
        try:
            path.write_text(original.replace(fixed, reverted, 1),
                            encoding="utf-8", newline="\n")
            code = run_tests(tf)
            caught = code != 0
            emit(f"\n[{mid}] 回退 → {tf} exit={code} → "
                 f"{'捕获 ✓' if caught else '存活 ✗（无判别力）'}")
            emit(f"    理由: {why}")
            results.append({"id": mid, "target": target, "test": tf,
                            "exit_code": code,
                            "result": "caught" if caught else "survived",
                            "why": why})
        finally:
            path.write_text(original, encoding="utf-8", newline="\n")

    after = all(run_tests(tf) == 0 for tf in baselines)
    emit(f"\n[复原校验] 全部回退后复跑 → {'全绿 ✓' if after else '非绿 ✗'}")
    caught_n = sum(1 for r in results if r["result"] == "caught")
    survived_n = sum(1 for r in results if r["result"] == "survived")
    missed_n = sum(1 for r in results if r["result"] == "anchor_missed")
    emit(f"结论：捕获 {caught_n} / 存活 {survived_n} / 锚点未命中 {missed_n}")
    emit("=" * 70)

    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (LOG.parent / "post-codex-fix-mutation-results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if survived_n == 0 and missed_n == 0 and after else 1


if __name__ == "__main__":
    raise SystemExit(main())
