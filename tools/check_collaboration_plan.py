"""Validate collaboration metadata; optionally render its human-readable board.

This checks planning consistency, not implementation readiness or acceptance.
"""
import argparse
import hashlib
import json
import re
import subprocess
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
        "## 基线", "", f"基线记录锚点：`{board['baseline']['head_commit']}`（记录时的提交，不是动态 HEAD）。",
        f"基线状态：`{board['baseline']['status']}`；已发布 commit：{board['baseline']['published_commit'] or '尚未发布'}。",
        f"历史共享工作区验证为服务端 {result['python_passed']} passed / {result['python_skipped']} skipped、"
        f"客户端 {result['client_passed']} passed；不是纯 HEAD 的复现结果。", "",
        "## 子任务认领与交付", "", "| 子任务 | 建议开发线 | 负责人 | 审阅人 | 状态 | 接手卡 |",
        "|---|---|---|---|---|---|"]
    candidate = board["baseline"].get("last_verified_candidate")
    if candidate:
        index = lines.index("## 子任务认领与交付")
        lines[index:index] = [f"最近独立候选验证：服务端 **{candidate['python_passed']} passed / "
            f"{candidate['python_skipped']} skipped**；源码提交/父提交："
            f"`{candidate.get('commit') or candidate.get('parent_commit')}`，tree：`{candidate['source_tree']}`。",
            candidate.get("scope", ""), ""]
    verification = board["baseline"].get("latest_source_verification")
    if verification:
        index = lines.index("## 子任务认领与交付")
        lines[index:index] = [f"最近提交源码核验（不是新发布候选）：`{verification['source_commit']}`；"
            f"后端 **{verification['python_passed']} passed / {verification['python_skipped']} skipped**，"
            f"客户端 **{verification['client_passed']} passed**，类型检查通过。",
            f"范围：{verification['scope']}；[核验证据]({verification['evidence']})。", ""]
    execution = board.get("execution_queue")
    if execution:
        index = lines.index("## 子任务认领与交付")
        queue_lines = ["## 当前开发队列", "",
            f"执行方式：{execution['mode']}；当前开发者：{execution['owner']}；同时开发上限：{execution['wip_limit']} 张卡。",
            f"正在开发：{execution['active_task'] or '无（下一张尚未开工）'}；下一张：{execution['next_task']}。",
            "claimed 表示已接手，排队项不表示已经开工；review 保留原作者，未分配独立审阅人不代表可以自审。", "",
            "| 顺序 | 子任务 | 调度状态 | 开工/交付条件 |", "|---|---|---|---|"]
        for index_number, item in enumerate(execution["items"], 1):
            queue_lines.append(f"| {index_number} | {item['id']} | {item['state']} | {item['condition']} |")
        queue_lines += ["", "独立复核与基线发布另列，不占用开发队列，也不阻止基于已交付接口准备后续实现。", ""]
        lines[index:index] = queue_lines
    for card in board["dispatches"]:
        lines.append(f"| {card['id']} | {roles[card['stream']]} | {card['owner'] or '待认领'} | "
            f"{card['reviewer'] or '待定'} | {card['status']} | [{card['title']}]({card['card'].split('docs/协作开发/', 1)[1]}) |")
    lines += ["", board.get("execution_note", "基线发布之前，非集成任务只准备接口、夹具和独立草稿；接线、集成与验收等待共同基线和原工作包依赖。"),
        "", "## 23 个工作包总账", "", "planned 表示整包后续范围待实施；已交付切片单独记录为局部已验证，不表示已独立验收。",
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
        "- branch/base_commit：实际分支及接手源码锚点；注明是否已发布，不把接手HEAD冒充已发布共同基线。",
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
        authorization = item.get("review_authorization") or {}
        user_revalidation = (authorization.get("source") == "user_request"
            and authorization.get("allow_same_author") is True
            and authorization.get("independent") is False
            and bool(authorization.get("request"))
            and bool(item.get("acceptance_review", {}).get("report")))
        if item["reviewer"] and item["reviewer"] == item["owner"] and not user_revalidation:
            errors.append("task author cannot be their own reviewer: " + item["id"])
        if not set(item["shared_files"]).issubset(paths):
            errors.append("undeclared shared-file handoff: " + item["id"])
    baseline = board["baseline"]
    execution = board.get("execution_queue")
    if execution:
        queue_ids = [item["id"] for item in execution["items"]]
        if len(queue_ids) != len(set(queue_ids)) or not set(queue_ids).issubset(dispatch_ids):
            errors.append("execution queue requires unique known dispatch ids")
        if execution.get("next_task") not in queue_ids:
            errors.append("next task must belong to execution queue")
        active_task = execution.get("active_task")
        if execution.get("wip_limit") != 1 or (active_task and active_task not in queue_ids):
            errors.append("single-developer queue requires WIP=1 and a known active task")
        queued = [item for item in dispatches if item["id"] in queue_ids]
        if any(item["owner"] != execution["owner"] or item["status"] not in {"claimed", "blocked", "in_progress"} for item in queued):
            errors.append("queue tasks must belong to the developer and remain pending implementation")
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


def verify_prepared(board):
    """Verify the pinned Git object and evidence files, independent of dirty workspaces."""
    prepared = board["baseline"].get("prepared_candidate") or {}
    commit, tree = prepared.get("commit", ""), prepared.get("source_tree", "")
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) for value in (commit, tree)):
        return ["prepared candidate requires exact commit and tree"]
    result = subprocess.run(["git", "rev-parse", commit + "^{tree}"], cwd=ROOT,
                            capture_output=True, text=True)
    errors = []
    if result.returncode or result.stdout.strip() != tree:
        errors.append("prepared candidate Git tree does not match commit")
    for evidence in prepared.get("evidence", []):
        path = ROOT / evidence["path"]
        data = path.read_bytes() if path.is_file() else None
        # Git archive/checkouts may apply core.autocrlf. Only explicitly marked
        # text evidence uses the canonical LF body; legacy evidence stays raw.
        if data is not None and evidence.get("sha256_basis") == "lf_normalized_text":
            data = data.replace(b"\r\n", b"\n")
        if data is None or hashlib.sha256(data).hexdigest() != evidence["sha256"]:
            errors.append("prepared evidence changed: " + evidence["path"])
    if not prepared.get("evidence"):
        errors.append("prepared candidate is missing verification evidence")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--verify-snapshot", action="store_true")
    parser.add_argument("--verify-prepared", action="store_true")
    args = parser.parse_args()
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    if args.render:
        RENDERED.write_text(render(board), encoding="utf-8", newline="\n")
    errors = validate(board)
    if args.verify_snapshot:
        errors.extend(verify_snapshot(board))
    if args.verify_prepared:
        errors.extend(verify_prepared(board))
    print(json.dumps({"scope": "collaboration metadata only", "workpackages": len(board["workpackages"]),
        "dispatches": len(board["dispatches"]), "baseline": board["baseline"]["status"],
        "errors": errors, "ok": not errors}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
