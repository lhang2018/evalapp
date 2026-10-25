"""Curate the two numbering series and product topics without changing workboard."""
import hashlib
import json
from pathlib import Path
import re

root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent
audit = json.loads((out / "corpus-audit.json").read_text(encoding="utf-8"))
continuation = json.loads((out / "continuation-designs.json").read_text(encoding="utf-8"))
turns = {t["qa"]: t for t in audit["turns"]}
finals = {m["id"]: m for m in audit["finals"]}
technical_turns = [114,117,118,120,121,122,123,124,125,126,127,128,129,131,132,133,134,135,136,137,138,139,145,146]
technical_titles = [
 "多专家协同执行、冲突解决与协作协议", "意图理解、目标澄清与任务契约", "执行可靠性、工具契约与结果真实性",
 "工具能力注册、发现与兼容替代", "自主进化变更治理与安全发布", "收益归因、经验固化与能力资产化",
 "能力缺口发现与主动能力规划", "能力组合、跨专家协作与动态团队", "结果验收、事实核验与可信交付",
 "证据完整性、记忆可信度与评测数据", "变更影响、因果归因与安全发布", "自主权限、决策边界与可验证控制",
 "运行时不变量、故障隔离与安全恢复", "可观测性、事件关联与诊断数据质量", "修复有效性与长期收益验证",
 "进化决策、改进优先级与资源分配", "知识沉淀、经验迁移与能力复用", "可组合能力、动态工作流与协同",
 "跨设备任务状态、断联恢复与幂等", "运行时安全边界与高风险控制", "用户数据、隐私与记忆隔离",
 "质量评测、因果归因与持续改进", "核心执行优先调度与资源隔离", "模型、工具与运行环境兼容治理",
]
technical_aln = [
 ["ALN-07","ALN-18"],["ALN-01","ALN-07","ALN-13"],["ALN-01","ALN-02","ALN-07"],
 ["ALN-03","ALN-06","ALN-18"],["ALN-09","ALN-10","ALN-11"],["ALN-05","ALN-10","ALN-14"],
 ["ALN-13","ALN-14"],["ALN-07","ALN-18"],["ALN-10","ALN-16"],["ALN-05","ALN-10","ALN-16"],
 ["ALN-06","ALN-10","ALN-11"],["ALN-01","ALN-13","ALN-14"],["ALN-02","ALN-07","ALN-17"],
 ["ALN-12","ALN-16"],["ALN-09","ALN-10"],["ALN-14","ALN-15"],["ALN-05","ALN-06"],
 ["ALN-07","ALN-18"],["ALN-02","ALN-12","ALN-21"],["ALN-01","ALN-02","ALN-09"],
 ["ALN-05","ALN-16"],["ALN-09","ALN-10"],["ALN-14","ALN-15"],["ALN-03","ALN-06","ALN-20"],
]
later_aln = [
 ["ALN-03","ALN-06","ALN-20"],["ALN-06","ALN-10"],["ALN-10","ALN-22"],
 ["ALN-09","ALN-10"],["ALN-10","ALN-11","ALN-16"],["ALN-09","ALN-16"],
 ["ALN-05","ALN-06"],["ALN-05","ALN-07"],["ALN-13","ALN-14","ALN-15"],
 ["ALN-01","ALN-07","ALN-13"],["ALN-07"],["ALN-07","ALN-16"],["ALN-09","ALN-16"],
 ["ALN-05","ALN-06"],["ALN-05"],["ALN-05","ALN-13","ALN-14"],["ALN-13","ALN-14","ALN-15"],
 ["ALN-04","ALN-12","ALN-16"],["ALN-01","ALN-12"],["ALN-01","ALN-07","ALN-10"],
 ["ALN-09","ALN-16"],["ALN-09","ALN-10","ALN-22"],["ALN-10","ALN-12","ALN-16"],
]
rows = []
for number, qa, title, aln in zip(range(53,77), technical_turns, technical_titles, technical_aln):
    final = finals[turns[qa]["final_ids"][0]]
    rows.append(dict(key=f"A-{number}", series="早期技术编号", number=number, title=title, user_qa_label=qa,
        message_id=final["id"], utc=final["utc"], source_sha256=final["sha256"],
        related_aln=aln, mapping_status="proposed_coverage_not_accepted", implementation_status="requires_gap_review"))
for row, aln in zip(continuation, later_aln):
    assert row["number"] is not None
    final = finals[row["id"]]
    rows.append(dict(key=f"B-{row['number']}", series="后续续编编号", number=row["number"], title=row["title"],
        user_qa_label=row["qa"], message_id=row["id"], utc=final["utc"], source_sha256=final["sha256"],
        related_aln=aln, mapping_status="proposed_coverage_not_accepted", implementation_status="requires_gap_review"))
assert len(rows) == 47 and len({row["message_id"] for row in rows}) == 47
for row in rows:
    source = (root / "outputs/readme-design-review-06d5f7e/finals" / (row["message_id"] + ".md")).read_text(encoding="utf-8")
    heading = re.search(r"<WritingBlock[^>]*>\s*#\s+([^\n]+)", source)
    if heading is None:
        heading = re.search(r"^#\s+([^\n]+)", source, re.MULTILINE)
    assert heading, row["key"]
    row["source_title"] = heading.group(1).strip()
product_qas = [140,141,142,143,144,147,148,149,150,151,152,153,154,155,156,157]
products = []
for qa in product_qas:
    final = finals[turns[qa]["final_ids"][0]]
    products.append(dict(qa=qa, message_id=final["id"], utc=final["utc"], source_sha256=final["sha256"],
        headings=final["headings"], classification="unnumbered_product_discussion"))
assert len({p["message_id"] for p in products} | {r["message_id"] for r in rows}) == 63
state = json.loads((root / "docs/协作开发/workboard.json").read_text(encoding="utf-8"))
valid = {p["id"] for p in state["workpackages"]}
assert all(set(r["related_aln"]) <= valid for r in rows)
report = dict(scope="47 numbered source documents plus 16 product discussions; not 47 new implemented features",
    technical_series=24, continuation_series=23, unnumbered_product_discussions=16,
    numbering_collisions=[75,76], unseen_body_from=98, rows=rows, product_discussions=products)
(out / "design-mapping.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
manifest = json.loads((root / "chat/原稿归档/MANIFEST_2026-10-25.json").read_text(encoding="utf-8"))
archive = root / "chat/原稿归档/2026-10-25"
errors = []
for item in manifest["items"]:
    path = archive / item["path"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        errors.append(item["path"])
(out / "archive-verification.json").write_text(json.dumps(dict(files=len(manifest["items"]), errors=errors,
    verified=len(manifest["items"])-len(errors), scope="archived local bytes only, not whole-conversation completeness"), indent=2),
    encoding="utf-8", newline="\n")
print(json.dumps(dict(numbered_sources=len(rows), product_discussions=len(products), archive_verified=len(manifest["items"])-len(errors)), indent=2))
