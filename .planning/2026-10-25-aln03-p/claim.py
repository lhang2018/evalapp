"""ALN-03-P 认领登记：只改 03-P 一条，精确文本替换，双份（工作区+HEAD快照）。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "docs/协作开发/workboard.json"
TMP = Path("E:/tmp-03p-workboard.json")
COMMIT = "a038f2195ab8bc5d571900fdf27a14e1400adf5b"

OLD = '''      "id": "ALN-03-P",
      "workpackage": "ALN-03",
      "title": "生产信任根与元数据制品联验",
      "stream": "assets",
      "owner": null,
      "reviewer": null,
      "branch": null,
      "base_commit": null,
      "status": "unclaimed",
      "blocker": null,
      "evidence": [],'''

NEW = '''      "id": "ALN-03-P",
      "workpackage": "ALN-03",
      "title": "生产信任根与元数据制品联验",
      "stream": "assets",
      "owner": "claude-main",
      "reviewer": null,
      "branch": "main",
      "base_commit": "''' + COMMIT + '''",
      "status": "review",
      "blocker": null,
      "evidence": [
        "docs/协作开发/交付/ALN-03-P-生产信任根与制品联验.md",
        "evoagent/release/identity.py",
        "tests/test_release_identity.py：30 项通过（含真实端侧 rsa.ts 联验）",
        ".planning/2026-10-25-aln03-p/mutation_test.py：变异 5/5 捕获",
        "跨端契约缺陷实锤：format(65537,'x')='10001' 被 Node 截断/端侧 base64url 解错，_hex_even 修复"
      ],'''


def apply(text, label):
    count = text.count(OLD)
    if count != 1:
        print(f"✗ {label}：锚点出现 {count} 次，要求恰好 1 次")
        return None
    return text.replace(OLD, NEW, 1)


head = subprocess.run(["git", "show", "HEAD:docs/协作开发/workboard.json"],
                      capture_output=True, cwd=str(ROOT))
head_text = head.stdout.decode("utf-8")
updated_head = apply(head_text, "HEAD 快照")
if updated_head is None:
    sys.exit(1)
TMP.write_text(updated_head, encoding="utf-8", newline="")

work_text = BOARD.read_text(encoding="utf-8")
updated_work = apply(work_text, "工作区文件")
if updated_work is None:
    sys.exit(1)
BOARD.write_text(updated_work, encoding="utf-8", newline="")
print("✓ 工作区与 HEAD 快照均已只改 03-P 一条")
