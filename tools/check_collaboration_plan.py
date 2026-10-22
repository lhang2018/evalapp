"""Validate collaboration metadata; optionally render its human-readable board.

This checks planning consistency, not implementation readiness or acceptance.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "docs/协作开发/workboard.json"
RENDERED = BOARD.with_name("开发任务认领表.md")
ORIGINAL = ROOT / ".planning/2026-10-22-design-alignment/implementation-workpackages.json"
CLAIM_STATES = {"unclaimed", "claimed", "in_progress", "review", "merged", "accepted", "blocked"}
DELIVERY_STATES = {"partial_verified", "needs_recheck", "planned"}


def render(board):
    labels = {"partial_verified": "局部已验证，整包未关闭", "needs_recheck": "有同期实现，先复核剩余范围",
              "planned": "整包后续排期"}
    roles = {entry["id"]: entry["name"] for entry in board["streams"]}
    result = board["baseline"]["last_verified_workspace"]
    lines = ["# 开发任务认领表", "", "本表由 workboard.json 生成；认领先改 JSON，再运行检查器的 --render。",
        "角色是建议职责，负责人以 workboard.json 认领为准，审阅人未分配时保留待定；认领状态不等于功能验收状态。", "",
        "## 基线", "", f"当前 HEAD：`{board['baseline']['head_commit']}`。",
        f"基线状态：`{board['baseline']['status']}`；已发布 commit：{board['baseline']['published_commit'] or '尚未发布'}。",
        f"工作区最近验证为服务端 {result['python_passed']} passed / {result['python_skipped']} skipped、"
        f"客户端 {result['client_passed']} passed；不是纯 HEAD 的复现结果。", "",
        "## 首轮认领", "", "| 子任务 | 建议开发线 | 负责人 | 审阅人 | 状态 | 接手卡 |",
        "|---|---|---|---|---|---|"]
    for card in board["dispatches"]:
        lines.append(f"| {card['id']} | {roles[card['stream']]} | {card['owner'] or '待认领'} | "
            f"{card['reviewer'] or '待定'} | {card['status']} | [{card['title']}]({card['card'].split('docs/协作开发/', 1)[1]}) |")
    lines += ["", "基线发布之前，非集成任务只准备接口、夹具和独立草稿；接线、集成与验收等待共同基线和原工作包依赖。",
        "", "## 23 个工作包总账", "", "planned 表示本次整包尚未开工，不表示仓库没有相关基础模块。",
        "needs_recheck 表示不能照历史快照重复开发；先核对已提交/在途行为再细化。", "",
        "| ALN | 优先级 | 工作包 | 当前进度 | 推荐协调角色 | 协调人 | 原始依赖 | 下一项工作 |",
        "|---|---|---|---|---|---|---|---|"]
    for package in board["workpackages"]:
        lines.append(f"| {package['id']} | {package['priority']} | {package['title']} | "
            f"{labels[package['delivery_state']]} | {roles[package['stream']]} | {package['owner'] or '待认领'} | "
            f"{', '.join(package['depends_on']) or '无'} | {package['next_step']} |")
    lines += ["", "每个工作包的完整交付与关闭标准仍以[原实施计划](../全量对齐与下一步实施计划_2026-10-22.md)为准。",
        "ALN-04-R 与 ALN-04-UI 是同一工作包内分离文件/职责的子任务，不能各自宣布父工作包已全部完成。",
        "后续细分需补任务卡、独占边界和认领记录，不能只填一句‘继续某模块’。", "",
        "## 认领字段", "", "- owner/reviewer：实际接手和审阅人；未分配保留 null。",
        "- branch/base_commit：实际分支及共同开发基线；未发布基线时不要填写旧 HEAD 冒充。",
        "- status：unclaimed → claimed → in_progress → review → merged → accepted；blocked 需写清 blocker。",
        "- evidence：命令、结果、日志与源码 commit；merged/accepted 必须有证据，accepted 必须有审阅人。",
        "- workpackages.owner：整包协调人；dispatches.owner：具体子任务开发者。同一包拆卡不代表同文件可并发覆盖。", ""]
    return "\n".join(lines)


def validate(board):
    errors = []
    original = json.loads(ORIGINAL.read_text(encoding="utf-8"))
    originals = {item[0]: item for item in original["workpackages"]}
    streams = [entry["id"] for entry in board["streams"]]
    packages = board["workpackages"]
    ids = [entry["id"] for entry in packages]
    if len(ids) != len(set(ids)) or set(ids) != set(originals) or len(ids) != 23:
        errors.append("workpackages must cover the original 23 ALN ids exactly once")
    if len(streams) != len(set(streams)):
        errors.append("duplicate stream ids")
    for item in packages:
        old = originals.get(item["id"])
        if old and (item["depends_on"] != old[3] or item["title"] != old[2]):
            errors.append(f"{item['id']}: changed original dependencies/title")
        if item["stream"] not in streams or item["delivery_state"] not in DELIVERY_STATES:
            errors.append(f"{item['id']}: invalid role or delivery state")
    graph = {item["id"]: item["depends_on"] for item in packages}
    visiting, visited = set(), set()

    def visit(identity):
        if identity in visiting:
            errors.append("dependency cycle at " + identity)
            return
        if identity in visited:
            return
        visiting.add(identity)
        for dep in graph.get(identity, []):
            if dep not in graph:
                errors.append("unknown dependency " + dep)
            else:
                visit(dep)
        visiting.remove(identity)
        visited.add(identity)

    for identity in graph:
        visit(identity)
    shared = board["shared_files"]
    paths = [item["path"] for item in shared]
    if len(paths) != len(set(paths)):
        errors.append("shared files have multiple integration owners")
    for item in shared:
        if item["stream"] not in streams or not (ROOT / item["path"]).is_file():
            errors.append("invalid shared-file owner/path: " + item["path"])
    dispatches = board["dispatches"]
    dispatch_ids = [item["id"] for item in dispatches]
    if len(dispatch_ids) != len(set(dispatch_ids)):
        errors.append("duplicate dispatch ids")
    for item in dispatches:
        if item["workpackage"] not in graph or item["stream"] not in streams:
            errors.append("unknown parent/stream for " + item["id"])
        card = ROOT / item["card"]
        if not card.is_file():
            errors.append("missing task card: " + item["card"])
        else:
            text = card.read_text(encoding="utf-8")
            if item["id"] not in text or any(x not in text for x in ("## 目标", "## 写入边界", "## 验收", "## 交付")):
                errors.append("incomplete task card: " + item["id"])
        if item["status"] not in CLAIM_STATES:
            errors.append("invalid claim status: " + item["id"])
        if item["status"] not in ("unclaimed", "blocked") and not item["owner"]:
            errors.append("claimed task requires a real owner: " + item["id"])
        if item["status"] == "blocked" and not item.get("blocker"):
            errors.append("blocked task requires a blocker: " + item["id"])
        if item["status"] in ("merged", "accepted") and not item["evidence"]:
            errors.append("merged/accepted task requires evidence: " + item["id"])
        if item["status"] == "accepted" and not item["reviewer"]:
            errors.append("accepted task requires a reviewer: " + item["id"])
        if not set(item["shared_files"]).issubset(paths):
            errors.append("undeclared shared-file handoff: " + item["id"])
    baseline = board["baseline"]
    if baseline["status"] not in ("candidate", "published"):
        errors.append("invalid baseline status")
    if baseline["status"] == "published" and not re.fullmatch(r"[0-9a-f]{40}", baseline["published_commit"] or ""):
        errors.append("published baseline requires a full commit id")
    if not (ROOT / baseline["snapshot"]).is_file():
        errors.append("missing candidate snapshot")
    if not RENDERED.is_file() or RENDERED.read_text(encoding="utf-8") != render(board):
        errors.append("Markdown board is stale; run --render")
    return errors


def verify_snapshot(board):
    snapshot = json.loads((ROOT / board["baseline"]["snapshot"]).read_text(encoding="utf-8"))
    errors = []
    for item in snapshot["files"] + snapshot["evidence"]:
        path = ROOT / item["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            errors.append("snapshot changed: " + item["path"])
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--verify-snapshot", action="store_true")
    args = parser.parse_args()
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    if args.render:
        RENDERED.write_text(render(board), encoding="utf-8", newline="\n")
    errors = validate(board)
    if args.verify_snapshot:
        errors.extend(verify_snapshot(board))
    print(json.dumps({"scope": "collaboration metadata only", "workpackages": len(board["workpackages"]),
        "dispatches": len(board["dispatches"]), "baseline": board["baseline"]["status"],
        "errors": errors, "ok": not errors}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
