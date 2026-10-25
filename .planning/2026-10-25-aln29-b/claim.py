"""ALN-29-B 认领登记：只改 29-B 一条，精确文本替换，不重写整份 JSON。

同时作用于工作区文件与 HEAD 快照（tmp），以便用 update-index 做外科手术式暂存。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "docs/协作开发/workboard.json"
TMP = Path("E:/tmp-29b-workboard.json")
COMMIT = "029a9987eeffff1fc502987f3d6a6bdfa8722ec4"

OLD = '''      "id": "ALN-29-B",
      "workpackage": "ALN-29",
      "title": "首发冻结范围与八道发布门禁",
      "stream": "integration",
      "owner": null,
      "reviewer": null,
      "branch": null,
      "base_commit": null,
      "status": "unclaimed",
      "blocker": null,
      "evidence": [],
      "card": "docs/协作开发/任务卡/29-B-首发冻结范围与八道发布门禁.md",
      "shared_files": [
        "tools/check_collaboration_plan.py"
      ],
      "requirement_batch": "PRODUCT-2026-10-25",
      "depends_on": [
        "ALN-29-A"
      ],
      "phase": 0,
      "requirement_refs": [
        "UX-26",
        "UX-27",
        "UX-28",
        "G0",
        "G1",
        "G2",
        "G3",
        "G4",
        "G5",
        "G6",
        "G7"
      ],
      "next_step": "按卡实施并提交证据；未经复核不记accepted",'''

NEW = '''      "id": "ALN-29-B",
      "workpackage": "ALN-29",
      "title": "首发冻结范围与八道发布门禁",
      "stream": "integration",
      "owner": "claude-main",
      "reviewer": null,
      "branch": "main",
      "base_commit": "''' + COMMIT + '''",
      "status": "review",
      "blocker": null,
      "evidence": [
        "docs/协作开发/验收/ALN-29-B-首发冻结范围与八道发布门禁.md",
        "docs/协作开发/验收/首发门禁证据_2026-10-25.json",
        "tools/check_release_gates.py",
        "tests/test_release_gates.py：14 项通过",
        ".planning/2026-10-25-aln29-b/mutation_test.py：变异 7/7 捕获",
        "干净 worktree 固定源码 029a998：pytest 1675 passed / 1 skipped"
      ],
      "card": "docs/协作开发/任务卡/29-B-首发冻结范围与八道发布门禁.md",
      "shared_files": [
        "tools/check_collaboration_plan.py"
      ],
      "requirement_batch": "PRODUCT-2026-10-25",
      "depends_on": [
        "ALN-29-A"
      ],
      "phase": 0,
      "requirement_refs": [
        "UX-26",
        "UX-27",
        "UX-28",
        "G0",
        "G1",
        "G2",
        "G3",
        "G4",
        "G5",
        "G6",
        "G7"
      ],
      "next_step": "开发完成转 review；审阅人未定，不自批为独立验收；未触碰 ALN-29-A 在途的 tools/check_collaboration_plan.py",'''


def apply(text, label):
    count = text.count(OLD)
    if count != 1:
        print(f"✗ {label}：锚点出现 {count} 次，要求恰好 1 次")
        return None
    return text.replace(OLD, NEW, 1)


# 1) HEAD 快照
head = subprocess.run(["git", "show", "HEAD:docs/协作开发/workboard.json"],
                      capture_output=True, cwd=str(ROOT))
head_text = head.stdout.decode("utf-8")
updated_head = apply(head_text, "HEAD 快照")
if updated_head is None:
    sys.exit(1)
TMP.write_text(updated_head, encoding="utf-8", newline="")

# 2) 工作区文件
work_text = BOARD.read_text(encoding="utf-8")
updated_work = apply(work_text, "工作区文件")
if updated_work is None:
    sys.exit(1)
BOARD.write_text(updated_work, encoding="utf-8", newline="")

print("✓ 工作区文件与 HEAD 快照均已只改 29-B 一条")
