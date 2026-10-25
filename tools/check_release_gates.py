"""校验首发冻结范围与发布门禁证据表。

只做两件事，刻意分开：
1. 结构与自洽校验（本脚本默认行为）：证据表本身是否完整、状态与证据是否自洽、
   与协作台账的事实是否矛盾。退出码 0/1。
2. 发布条件判定（--require-release）：八道门禁与十三项首发范围是否全部通过。
   退出码 0/2。

本脚本不执行任何被声称已通过的验证，也不把“未运行”计为通过。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / "docs/协作开发/验收/首发门禁证据_2026-10-25.json"
DEFAULT_BOARD = ROOT / "docs/协作开发/workboard.json"

GATE_IDS = [f"G{i}" for i in range(8)]
RANGE_IDS = [f"R{i:02d}" for i in range(1, 14)]
STATUSES = {"pass", "fail", "untested"}
EVIDENCE_KEYS = ("kind", "ref", "result")


def load_json(path):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} 不是合法 JSON：{error}") from error


def check_item(item, errors, label):
    """校验一个门禁或范围项的状态与证据是否自洽。"""
    item_id = item.get("id", "<无 id>")
    status = item.get("status")
    evidence = item.get("evidence")
    if status not in STATUSES:
        errors.append(f"{label} {item_id}：status 必须是 {sorted(STATUSES)} 之一，实际为 {status!r}")
        return
    if evidence is None:
        errors.append(f"{label} {item_id}：缺少 evidence 字段（未测试也必须显式写空列表）")
        return
    if status == "pass":
        if not evidence:
            errors.append(f"{label} {item_id}：标记为 pass 但没有证据；未运行不得记为通过")
        for index, entry in enumerate(evidence, 1):
            missing = [key for key in EVIDENCE_KEYS if not str(entry.get(key, "")).strip()]
            if missing:
                errors.append(f"{label} {item_id}：第 {index} 条证据缺少 {', '.join(missing)}")
    elif status == "fail":
        if not str(item.get("reason", "")).strip():
            errors.append(f"{label} {item_id}：标记为 fail 但没有写明 reason")
    else:  # untested
        if evidence:
            errors.append(f"{label} {item_id}：标记为 untested 却挂了证据，状态与证据矛盾")


def validate(evidence, board):
    errors = []
    for key in ("id", "frozen_range", "gates", "first_task_chain", "delivery_package"):
        if key not in evidence:
            errors.append(f"证据表缺少顶层字段 {key}")
    if errors:
        return errors

    frozen = evidence["frozen_range"]
    required = frozen.get("required", [])
    range_ids = [item.get("id") for item in required]
    if range_ids != RANGE_IDS:
        missing = [rid for rid in RANGE_IDS if rid not in range_ids]
        extra = [rid for rid in range_ids if rid not in RANGE_IDS]
        errors.append(f"首发范围必须恰好是 R01—R13（顺序一致）；缺少 {missing}，多余 {extra}")
    for item in required:
        for key in ("name", "release_requirement", "on_violation", "status"):
            if not str(item.get(key, "")).strip():
                errors.append(f"首发范围 {item.get('id')}：缺少 {key}")
        check_item(item, errors, "首发范围")

    gates = evidence.get("gates", [])
    gate_ids = [gate.get("id") for gate in gates]
    if gate_ids != GATE_IDS:
        missing = [gid for gid in GATE_IDS if gid not in gate_ids]
        extra = [gid for gid in gate_ids if gid not in GATE_IDS]
        errors.append(f"发布门禁必须恰好是 G0—G7（顺序一致）；缺少 {missing}，多余 {extra}")
    for gate in gates:
        for key in ("name", "must_prove", "failure_handling", "status"):
            if not gate.get(key):
                errors.append(f"门禁 {gate.get('id')}：缺少 {key}")
        check_item(gate, errors, "门禁")

    published = ((board or {}).get("baseline") or {}).get("published_commit")
    for gate in gates:
        if gate.get("id") == "G6" and gate.get("status") == "pass" and not published:
            errors.append("门禁 G6 标记为 pass，但台账 baseline.published_commit 为空；"
                          "没有已发布版本就无法证明“已发布版本可以回滚”")

    delivery = evidence.get("delivery_package")
    if not isinstance(delivery, list) or not delivery:
        errors.append("delivery_package 必须是非空列表（发布前必须具备的交付包）")
    return errors


def summarize(evidence, board):
    gates = evidence.get("gates", [])
    required = evidence.get("frozen_range", {}).get("required", [])
    passed = [gate["id"] for gate in gates if gate.get("status") == "pass"]
    failed = [gate["id"] for gate in gates if gate.get("status") == "fail"]
    untested = [gate["id"] for gate in gates if gate.get("status") == "untested"]
    blocking = [gate["id"] for gate in gates
                if gate.get("hard_blocking") and gate.get("status") == "fail"]
    range_passed = [item["id"] for item in required if item.get("status") == "pass"]
    range_missing = [item["id"] for item in required if item.get("status") != "pass"]
    return {
        "gates_passed": passed,
        "gates_failed": failed,
        "gates_untested": untested,
        "blocking_failures": blocking,
        "range_passed": range_passed,
        "range_not_passed": range_missing,
        "published_commit": ((board or {}).get("baseline") or {}).get("published_commit"),
        "release_ready": not failed and not untested and not range_missing and not blocking,
    }


def report(evidence, board, summary):
    lines = ["首发冻结范围与发布门禁校验", "=" * 64,
             f"证据表：{evidence.get('id')}（更新于 {evidence.get('updated_at')}）",
             f"来源：{evidence.get('source', {}).get('doc', '未标注')}", ""]
    lines.append("一、发布门禁 G0—G7")
    lines.append("-" * 64)
    for gate in evidence.get("gates", []):
        mark = "硬性" if gate.get("hard_blocking") else "非硬性"
        count = len(gate.get("evidence") or [])
        lines.append(f"  {gate['id']} {gate.get('name', '')}：{gate.get('status')} "
                     f"[{mark}｜证据 {count} 条]")
        if gate.get("status") == "untested" and gate.get("observed"):
            lines.append(f"       线索（不计为证据）：{gate['observed']}")
    lines += ["", "二、首发必须交付范围 R01—R13"]
    lines.append("-" * 64)
    for item in evidence.get("frozen_range", {}).get("required", []):
        lines.append(f"  {item['id']} {item.get('name', '')}：{item.get('status')} "
                     f"（不满足时：{item.get('on_violation', '')}）")
    lines += ["", "三、发布决定", "-" * 64]
    lines.append(f"  门禁通过：{', '.join(summary['gates_passed']) or '无'}")
    lines.append(f"  门禁失败：{', '.join(summary['gates_failed']) or '无'}")
    lines.append(f"  门禁未测试：{', '.join(summary['gates_untested']) or '无'}")
    lines.append(f"  硬性门禁失败：{', '.join(summary['blocking_failures']) or '无'}")
    lines.append(f"  范围未通过：{', '.join(summary['range_not_passed']) or '无'}")
    lines.append(f"  台账 published_commit：{summary['published_commit'] or '尚未发布'}")
    lines.append(f"  结论：{'可宣布首发可用' if summary['release_ready'] else '不可宣布首发可用'}")
    if not summary["release_ready"]:
        lines.append("  说明：未测试与未通过都记为不通过；不得用综合分或其他门禁的提升抵消。")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="校验首发冻结范围与发布门禁证据表")
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE, help="证据表路径")
    parser.add_argument("--board", type=Path, default=DEFAULT_BOARD, help="协作台账路径")
    parser.add_argument("--require-release", action="store_true",
                        help="发布条件判定：八道门禁与 R01—R13 全部通过才退出 0，否则退出 2")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    args = parser.parse_args()

    try:
        evidence = load_json(args.evidence)
        board = load_json(args.board)
    except ValueError as error:
        print(f"✗ {error}")
        return 1
    if evidence is None:
        print(f"✗ 证据表不存在：{args.evidence}")
        return 1

    errors = validate(evidence, board)
    summary = summarize(evidence, board)

    if args.json:
        print(json.dumps({"errors": errors, "summary": summary}, ensure_ascii=False, indent=2))
    else:
        if errors:
            print("✗ 证据表结构与自洽校验未通过：")
            for error in errors:
                print(f"  - {error}")
            print()
        else:
            print("✓ 证据表结构与自洽校验通过（不代表任何门禁已通过）")
            print()
        print(report(evidence, board, summary))

    if errors:
        return 1
    if args.require_release and not summary["release_ready"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
