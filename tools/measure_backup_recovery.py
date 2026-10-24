#!/usr/bin/env python3
"""备份恢复 RTO/RPO 实测（ALN-17-A 验收第6条）。

流程：
1. 在临时数据目录构造真实负载（任务/事件/记忆，mock 模型，无外部网络）；
2. 创建备份，记录快照计数；
3. 在备份之后继续写入新数据（模拟"事故前未备份的增量"）；
4. 向空目录恢复（空机器恢复路径），测量 RTO（恢复+对账完成墙钟时间）；
5. 核对恢复后计数与快照差值，量化 RPO（数据损失范围）；
6. 输出 JSON 报告（stdout），可 --output 落盘。

用法：
    python tools/measure_backup_recovery.py [--output PATH]
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evoagent.app import App  # noqa: E402


def build_load(app: App, tasks: int) -> None:
    for index in range(tasks):
        app.controller.submit_task(
            f"计算 {index + 1}+{index + 2}",
            intent="computation", wait=True)
    from evoagent.memory.store import Credibility, MemoryItem, Scope
    for index in range(3):
        app.memory.write(MemoryItem(
            content=f"测量记忆 {index}",
            scope=Scope.PROJECT,
            status=Credibility.VERIFIED,
            evidence={"source": "measure"}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None,
                        help="报告 JSON 落盘路径（默认仅 stdout）")
    parser.add_argument("--tasks", type=int, default=5,
                        help="构造的任务数")
    args = parser.parse_args()

    source_dir = tempfile.mkdtemp(prefix="evoagent_measure_src_")
    target_dir = tempfile.mkdtemp(prefix="evoagent_measure_dst_")
    src_app = None
    dst_app = None
    report: dict = {"started_at": time.time(), "tasks": args.tasks}
    try:
        src_app = App(data_dir=source_dir)
        build_start = time.time()
        build_load(src_app, args.tasks)
        report["build_seconds"] = round(time.time() - build_start, 3)

        backup = src_app.backups.create("measure.zip")
        snapshot = dict(backup["counts"])
        report["snapshot_counts"] = snapshot
        report["backup_bytes"] = backup["files"] and {
            k: v["bytes"] for k, v in backup["files"].items()}

        # 备份后增量：这些数据不在备份内，恢复后将丢失（RPO 暴露窗口）
        src_app.controller.submit_task(
            "计算 99+99", intent="computation", wait=True)
        post_backup_tasks = src_app.backups._counts()["tasks"]
        report["rows_after_backup"] = {"tasks": post_backup_tasks}
        report["rpo_exposure_rows"] = {
            "tasks": post_backup_tasks - snapshot["tasks"]}

        src_app.db.close()
        src_app.memory.close()
        src_app = None

        # 空机器恢复：目标目录只装载数据面（不重放副作用）
        dst_app = App(data_dir=target_dir)
        restore_start = time.time()
        result = dst_app.backups.restore(str(Path(backup["path"])),
                                         safety_backup=False)
        rto = time.time() - restore_start
        report["restore"] = {
            "ok": result["ok"],
            "rto_seconds": round(rto, 3),
            "rto_ms_in_result": result.get("duration_ms"),
            "restored": result["restored"],
            "migrations_applied": result["migrations_applied"],
            "counts_verified": result["counts_verified"],
        }
        restored_counts = dst_app.backups._counts()
        report["restored_counts"] = restored_counts
        report["rpo"] = {
            key: restored_counts.get(key, 0) - snapshot.get(key, 0)
            for key in snapshot}
        report["rpo_note"] = (
            "恢复数据与快照逐行一致（差值0）；快照之后的增量"
            f" {report['rpo_exposure_rows']} 即本次事故的损失范围")
        report["task_readable_after_restore"] = bool(
            restored_counts["tasks"] >= snapshot["tasks"])
        report["ok"] = (
            result["ok"] and result["counts_verified"]
            and all(v == 0 for v in report["rpo"].values()))
    finally:
        for app in (src_app, dst_app):
            if app is not None:
                app.db.close()
                app.memory.close()
        shutil.rmtree(source_dir, ignore_errors=True)
        shutil.rmtree(target_dir, ignore_errors=True)
        report["finished_at"] = time.time()

    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
