"""变异测试：证明 tests/test_release_identity.py 有判别力。

对 identity.py 注入五处缺陷（隔离目录内进行），期望对应测试从通过变失败。
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "evoagent/release/identity.py"
TEST = ROOT / "tests/test_release_identity.py"
RSA_TS = ROOT / "android-app/src/update/rsa.ts"

MUTANTS = [
    ("M1 规范消息去掉前缀行（端侧对不齐）",
     '    return "\\n".join([\n        MESSAGE_PREFIX, schema, release_id, version,\n'
     '        digest, str(artifact_size), signing_key_id,\n    ]).encode("utf-8")',
     '    return "\\n".join([\n        schema, release_id, version,\n'
     '        digest, str(artifact_size), signing_key_id,\n    ]).encode("utf-8")',
     "test_seven_line_structure"),
    ("M2 篡改检测改为恒过（验签短路放行）",
     "    expected = pkcs1_v15_sha256_emsa(message, modulus.bit_length())\n"
     "    return encoded == expected",
     "    return True  # 变异：不再比较",
     "test_tampered_metadata_rejected"),
    ("M3 digest 归一去掉小写化",
     'digest = str(artifact_sha256).lower()',
     'digest = str(artifact_sha256)',
     "test_digest_normalization"),
    ("M4 偶数长度补零去掉（跨端契约破坏）",
     'text = format(value, "x")\n    return text if len(text) % 2 == 0 else "0" + text',
     'text = format(value, "x")\n    return text',
     "test_hex_even_envelope"),
    ("M5 撤销记录改为静默不校验原因",
     'if self.revoked and not self.revoked_reason:\n'
     '            raise IdentityError("撤销身份必须写明原因")',
     'revoked_reason = revoked_reason or "unspecified"  # 变异：静默补默认',
     "test_revoke_requires_reason"),
]


def build(tmp: Path, src_text: str) -> None:
    shutil.copytree(ROOT / "evoagent", tmp / "evoagent",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (tmp / "tests").mkdir()
    shutil.copy(TEST, tmp / "tests/test_release_identity.py")
    # identity.py 用替换后的源码
    (tmp / "evoagent/release/identity.py").write_text(src_text, encoding="utf-8")
    # rsa.ts 及其完整依赖链（sha256.ts → bytes.ts）也复制，跨实现测试需要
    (tmp / "android-app/src/update").mkdir(parents=True)
    (tmp / "android-app/src/offline").mkdir(parents=True)
    shutil.copy(RSA_TS, tmp / "android-app/src/update/rsa.ts")
    shutil.copy(ROOT / "android-app/src/offline/sha256.ts",
                tmp / "android-app/src/offline/sha256.ts")
    shutil.copy(ROOT / "android-app/src/offline/bytes.ts",
                tmp / "android-app/src/offline/bytes.ts")


def run(tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_release_identity.py", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(tmp),
        timeout=300)


results = []
base = SRC.read_text(encoding="utf-8")
with tempfile.TemporaryDirectory(prefix="aln03p-mut-") as raw:
    d = Path(raw)
    build(d, base)
    baseline = run(d)
    results.append(("基线（未变异）", baseline.returncode == 0,
                    f"退出 {baseline.returncode}", ""))

for name, old, new, expect in MUTANTS:
    if old not in base:
        results.append((name, False, "未找到锚点", expect))
        continue
    mutated = base.replace(old, new, 1)
    with tempfile.TemporaryDirectory(prefix="aln03p-mut-") as raw2:
        d = Path(raw2)
        build(d, mutated)
        outcome = run(d)
        caught = expect in (outcome.stdout + outcome.stderr) \
            and outcome.returncode != 0
        results.append((name, caught,
                        f"退出 {outcome.returncode}",
                        expect if caught else f"未触发 {expect}"))

print("=" * 76)
print("变异测试：tests/test_release_identity.py 对 identity.py 缺陷的判别力")
print("=" * 76)
for name, ok, detail, extra in results:
    print(f"{'✓' if ok else '✗'} {name}：{detail} {extra}")
passed = sum(1 for r in results if r[1])
print("-" * 76)
print(f"{passed}/{len(results)} 被捕获")
sys.exit(0 if passed == len(results) else 1)
