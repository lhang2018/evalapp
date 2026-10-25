"""从当前状态（已认领、短哈希）原位改为完整哈希——工作区+HEAD快照双份。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "docs/协作开发/workboard.json"
TMP = Path("E:/tmp-03p-workboard.json")
FULL = "a038f2195ab8bc5d571900fdf27a14e1400adf5b"

# 工作区文件：把已存在的 "base_commit": "a038f21"（短）替换为完整
work = BOARD.read_text(encoding="utf-8")
work_new = work.replace('"base_commit": "a038f21",',
                        f'"base_commit": "{FULL}",', 1)
if work_new == work:
    print("✗ 工作区未找到短哈希锚点")
    sys.exit(1)
BOARD.write_text(work_new, encoding="utf-8", newline="")

# HEAD 快照：完整 OLD→NEW（从 unclaimed 到 review，含完整哈希）
head = subprocess.run(["git", "show", "HEAD:docs/协作开发/workboard.json"],
                      capture_output=True, cwd=str(ROOT)).stdout.decode("utf-8")
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
NEW = OLD.replace(
    '"owner": null,\n      "reviewer": null,\n      "branch": null,\n      "base_commit": null,\n      "status": "unclaimed",\n      "blocker": null,\n      "evidence": [],',
    f'"owner": "claude-main",\n      "reviewer": null,\n      "branch": "main",\n      "base_commit": "{FULL}",\n      "status": "review",\n      "blocker": null,\n      "evidence": [\n        "docs/协作开发/交付/ALN-03-P-生产信任根与制品联验.md",\n        "evoagent/release/identity.py",\n        "tests/test_release_identity.py：30 项通过（含真实端侧 rsa.ts 联验）",\n        ".planning/2026-10-25-aln03-p/mutation_test.py：变异 5/5 捕获",\n        "跨端契约缺陷实锤：format(65537,\'x\')=\'10001\' 被 Node 截断/端侧 base64url 解错，_hex_even 修复"\n      ],')
count = head.count(OLD)
if count != 1:
    print(f"✗ HEAD 锚点出现 {count} 次")
    sys.exit(1)
TMP.write_text(head.replace(OLD, NEW, 1), encoding="utf-8", newline="")
print("✓ 工作区（短→完整哈希）与 HEAD 快照（unclaimed→review）均已更新")
