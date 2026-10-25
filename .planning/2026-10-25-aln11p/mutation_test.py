"""ALN-11-P 变异测试：证明本卡测试有判别力。

对服务端（delivery.py / manager.py）与端侧（journal.ts / retire.ts）注入
缺陷，必须被对应测试捕获。基线（未变异）必须全绿；每个变异体必须让
指定测试失败——恒过变异（等价变异）视为无判别力，需改写。
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DELIVERY = ROOT / "evoagent/release/delivery.py"
MANAGER = ROOT / "evoagent/release/manager.py"
JOURNAL = ROOT / "android-app/src/install/journal.ts"
RETIRE = ROOT / "android-app/src/install/retire.ts"
SERVER_TEST = ROOT / "tests/test_release_install_retire.py"
TRUST_TS = ROOT / "android-app/src/update/trust.ts"

# (名称, 目标文件, 原锚点, 变异替换, 期望失败的测试完整 ID)
SERVER_MUTANTS = [
    ("S1 ACTIONS 去掉 retired",
     DELIVERY,
     '"health_check", "rolled_back", "retired")',
     '"health_check", "rolled_back")',
     "TestRetireReceipt::test_retired_receipt_clears_current_and_keeps_known_good"),
    ("S2 退役回执不清空当前版本",
     DELIVERY,
     '            if current == resolved_release:\n'
     '                current, target, status = None, None, "retired"',
     '            if current == resolved_release:\n'
     '                status = "retired"',
     "TestRetireReceipt::test_retired_receipt_clears_current_and_keeps_known_good"),
    ("S3 签名身份核对恒放行",
     DELIVERY,
     '            if identity_status not in ("usable", "no_registry"):\n'
     '                return {"ok": False,\n'
     '                        "error": f"制品签名身份不可信: {identity_status}",\n'
     '                        "installation": installation}',
     '            if False:\n'
     '                return {"ok": False,\n'
     '                        "error": f"制品签名身份不可信: {identity_status}",\n'
     '                        "installation": installation}',
     "TestTrustBinding::test_revoked_identity_blocks_activation"),
    ("S4 自报密钥不一致不拒绝",
     DELIVERY,
     '            if signing_key_id and signing_key_id != server_key_id:\n'
     '                return {"ok": False,\n'
     '                        "error": "回执签名密钥与发布清单不一致",\n'
     '                        "installation": installation}',
     '            if False:\n'
     '                return {"ok": False,\n'
     '                        "error": "回执签名密钥与发布清单不一致",\n'
     '                        "installation": installation}',
     "TestTrustBinding::test_client_claimed_key_mismatch_refused"),
    ("S5 已退役版本可被激活",
     DELIVERY,
     '        if action in TRUST_BOUND_ACTIONS and hasattr(\n'
     '                self.releases, "is_retired") \\\n'
     '                and self.releases.is_retired(resolved_release):',
     '        if False and action in TRUST_BOUND_ACTIONS and hasattr(\n'
     '                self.releases, "is_retired") \\\n'
     '                and self.releases.is_retired(resolved_release):',
     "TestGrayRollbackChain::test_retired_release_refused_as_gray_activation_target"),
    ("S6 verify 不检查退役",
     MANAGER,
     '        if self.is_retired(release_id):\n'
     '            # 退役版本不得通过验证，也不得作为发布/回滚/对账目标\n'
     '            return {"ok": False, "retired": True,',
     '        if False:\n'
     '            # 退役版本不得通过验证，也不得作为发布/回滚/对账目标\n'
     '            return {"ok": False, "retired": True,',
     "TestRetireLifecycle::test_retire_records_and_blocks_verify_deploy_rollback"),
    ("S7 对账不检查退役",
     DELIVERY,
     '            check("release_not_retired", not retired,',
     '            check("release_not_retired", True,',
     "TestRetireLifecycle::test_reconcile_blocked_when_active_release_retired"),
]

# (名称, 目标文件, 原锚点, 变异替换, 期望失败的测试名)
CLIENT_MUTANTS = [
    ("C1 未确认回执冒充已安装",
     JOURNAL,
     '      trust = unconfirmed ? "retired_unconfirmed" : "retired";\n'
     '    } else {\n'
     '      trust = unconfirmed ? "installed_unconfirmed" : "installed_confirmed";',
     '      trust = "retired";\n'
     '    } else {\n'
     '      trust = "installed_confirmed";',
     "断联未确认不得冒充已安装"),
    ("C2 退役不要求原因",
     RETIRE,
     '  if (!input.reason.trim()) {\n'
     '    return { ok: false, entry: null, error: "退役必须写明原因", duplicate: false };\n'
     '  }',
     '  if (false) {\n'
     '    return { ok: false, entry: null, error: "退役必须写明原因", duplicate: false };\n'
     '  }',
     "退役必须写明原因"),
    ("C3 重复退役不去重",
     RETIRE,
     '  if (pending) {\n'
     '    // 已有未确认退役条目：重放而非重复登记\n'
     '    return { ok: false, entry: pending, error: "退役回执待确认", duplicate: true };\n'
     '  }',
     '  if (false) {\n'
     '    // 已有未确认退役条目：重放而非重复登记\n'
     '    return { ok: false, entry: pending, error: "退役回执待确认", duplicate: true };\n'
     '  }',
     "重复退役返回待确认条目且不重复上报"),
    ("C4 confirm 不写确认状态",
     JOURNAL,
     '      if (entry.apply_id === applyId && !entry.server_confirmed) {\n'
     '        entry.server_confirmed = true;\n'
     '        entry.confirmed_at = this.now();\n'
     '        confirmed = entry;\n'
     '      }',
     '      if (entry.apply_id === applyId && !entry.server_confirmed) {\n'
     '        confirmed = entry;\n'
     '      }',
     "确认后为 installed_confirmed"),
]


def build_server(tmp: Path) -> None:
    shutil.copytree(ROOT / "evoagent", tmp / "evoagent",
                    ignore=shutil.ignore_patterns("__pycache__"),
                    dirs_exist_ok=True)
    (tmp / "tests").mkdir(exist_ok=True)
    shutil.copy(SERVER_TEST, tmp / "tests/test_release_install_retire.py")
    # 跨实现联验需要端侧 trust.ts 及其依赖链
    (tmp / "android-app/src/update").mkdir(parents=True, exist_ok=True)
    (tmp / "android-app/src/offline").mkdir(parents=True, exist_ok=True)
    shutil.copy(TRUST_TS, tmp / "android-app/src/update/trust.ts")
    shutil.copy(ROOT / "android-app/src/update/rsa.ts",
                tmp / "android-app/src/update/rsa.ts")
    shutil.copy(ROOT / "android-app/src/offline/sha256.ts",
                tmp / "android-app/src/offline/sha256.ts")
    shutil.copy(ROOT / "android-app/src/offline/bytes.ts",
                tmp / "android-app/src/offline/bytes.ts")


def run_server(tmp: Path, expect: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest",
         f"tests/test_release_install_retire.py::{expect}", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(tmp),
        timeout=300)


def build_client(tmp: Path) -> None:
    shutil.copytree(ROOT / "android-app/src", tmp / "android-app/src",
                    ignore=shutil.ignore_patterns("__pycache__"),
                    dirs_exist_ok=True)
    shutil.copytree(ROOT / "android-app/test", tmp / "android-app/test",
                    ignore=shutil.ignore_patterns("__pycache__"),
                    dirs_exist_ok=True)


def run_client(tmp: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", "--test", "test/install/journal.test.ts",
         "test/install/retire.test.ts"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(tmp / "android-app"), timeout=300)


def client_test_failed(output: str, expect: str) -> bool:
    """node --test 输出里该测试名出现在 not ok 行即失败。"""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("not ok") and expect in stripped:
            return True
    return False


def main() -> int:
    results = []
    with tempfile.TemporaryDirectory(prefix="aln11p-mut-") as raw:
        base_dir = Path(raw)
        # ---- 服务端基线 ----
        server_dir = base_dir / "server"
        server_dir.mkdir()
        build_server(server_dir)
        # 先证明基线全绿
        whole = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/test_release_install_retire.py", "-q",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(server_dir), timeout=300)
        results.append(("服务端基线（未变异）", whole.returncode == 0,
                        f"退出 {whole.returncode}",
                        whole.stdout.strip().splitlines()[-1]
                        if whole.stdout.strip() else ""))
        for name, target, old, new, expect in SERVER_MUTANTS:
            if old not in target.read_text(encoding="utf-8"):
                results.append((name, False, "未找到锚点", expect))
                continue
            build_server(server_dir)
            mutated = target.read_text(encoding="utf-8").replace(old, new, 1)
            (server_dir / target.relative_to(ROOT)).write_text(
                mutated, encoding="utf-8")
            done = run_server(server_dir, expect)
            caught = done.returncode != 0
            tail = (done.stdout.strip().splitlines() or [""])[-1]
            results.append((name, caught, f"退出 {done.returncode}", tail))

        # ---- 端侧基线 ----
        client_dir = base_dir / "client"
        client_dir.mkdir()
        build_client(client_dir)
        base_client = run_client(client_dir)
        ok = base_client.returncode == 0 and "# fail 0" in base_client.stdout
        results.append(("端侧基线（未变异）", ok,
                        f"退出 {base_client.returncode}", ""))
        for name, target, old, new, expect in CLIENT_MUTANTS:
            rel = target.relative_to(ROOT / "android-app")
            if old not in target.read_text(encoding="utf-8"):
                results.append((name, False, "未找到锚点", expect))
                continue
            build_client(client_dir)
            mutated = target.read_text(encoding="utf-8").replace(old, new, 1)
            (client_dir / "android-app" / rel).write_text(
                mutated, encoding="utf-8")
            done = run_client(client_dir)
            caught = client_test_failed(done.stdout, expect)
            tail = (done.stdout.strip().splitlines() or [""])[-1]
            results.append((name, caught,
                            f"退出 {done.returncode}", tail))

    print(f"{'变异体':<38} {'捕获':<6} 详情")
    print("-" * 78)
    all_caught = True
    for name, caught, detail, tail in results:
        flag = "✅" if caught else "❌"
        print(f"{name:<38} {flag:<6} {detail}")
        if tail:
            print(f"{'':<38}       └ {tail[:90]}")
        if not caught:
            all_caught = False
    print("-" * 78)
    print("结论:", "全部变异体被捕获（测试有判别力）" if all_caught
          else "存在未被捕获的变异体——测试存在盲区")
    return 0 if all_caught else 1


if __name__ == "__main__":
    raise SystemExit(main())
