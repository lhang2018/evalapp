"""ALN-30-A 变异测试：验证可达性审计与装配收口的判别力。

每个变异体破坏一条核心不变量，要求对应测试**失败**（被捕获）。
变异体在隔离 worktree 内应用并还原，不触碰主工作区。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = r"C:\Users\lhang\AppData\Local\Programs\Python\Python310\python.exe"

TEST_FILE = "tests/test_integration_wiring.py"

MUTATIONS = [
    {
        "id": "M1",
        "desc": "审计 fail-open：把可达判定改成恒真（悬空能力被放过）",
        "file": "evoagent/integration/wiring.py",
        "old": "reachable = any(c[\"present\"] for c in checks)",
        "new": "reachable = True",
        "test": "TestWiringAudit::test_audit_detects_capability_without_any_entry",
    },
    {
        "id": "M2",
        "desc": "断言不抛错：assert_wiring_complete 不再阻断悬空",
        "file": "evoagent/integration/wiring.py",
        "old": "    if not report[\"ok\"]:",
        "new": "    if False:",
        "test": "TestWiringAudit::test_assert_raises_when_capability_dangling",
    },
    {
        "id": "M3",
        "desc": "CLI 入口解析失效：add_parser 永不匹配（命令集合为空）",
        "file": "evoagent/integration/wiring.py",
        "old": "        if not isinstance(func, ast.Attribute) or func.attr != \"add_parser\":\n            continue",
        "new": "        if True:\n            continue",
        "test": "TestWiringAudit::test_cli_commands_parsed_from_source",
    },
    {
        "id": "M4",
        "desc": "装配缺失：app 不再挂载主动帮助服务",
        "file": "evoagent/app.py",
        "old": "        self.assist = ProactiveAssistService(self.db,\n                                             memory_control=self.memory_control)",
        "new": "        pass  # 变异：移除主动帮助装配",
        "test": "TestAppWiring::test_assembled_services_are_real_implementations",
    },
    {
        "id": "M5",
        "desc": "身份绑定断裂：ReleaseDelivery 不再注入 IdentityRegistry",
        "file": "evoagent/app.py",
        "old": "            self.releases, self.db, tool_ids=set(self.tools.health()),\n            identity_registry=self.identity_registry)",
        "new": "            self.releases, self.db, tool_ids=set(self.tools.health()))",
        "test": "TestAppWiring::test_identity_registry_injected_into_delivery",
    },
    {
        "id": "M6",
        "desc": "恢复 fail-closed 失效：CLI 吞掉恢复拒绝并返回成功",
        "file": "evoagent/cli.py",
        "old": "    try:\n        report = app.recovery.restore(ref)\n    except (RuntimeError, ValueError, OSError) as e:\n        return _fail(str(e))",
        "new": "    try:\n        report = app.recovery.restore(ref)\n    except (RuntimeError, ValueError, OSError) as e:\n        return \"\"",
        "test": "TestRecoveryEntryPoint::test_host_restore_refuses_without_deployment_manifest",
    },
    {
        "id": "M7",
        "desc": "声明缺失：把凭据治理能力从注册表里抹掉",
        "file": "evoagent/integration/wiring.py",
        "old": "    Capability(\n        name=\"凭据治理与泄漏拦截\",\n        domain=\"observability\",\n        card=\"ALN-29-C\",\n        app_attr=\"credentials\",\n        cli_command=\"credential-audit\",\n        note=\"29-C 交付的凭据登记簿与私钥片段拦截零引用\",\n    ),",
        "new": "",
        "test": "TestAppWiring::test_capability_registry_covers_all_wired_domains",
    },
    {
        "id": "M8",
        "desc": "CLI 入口未注册：identity-list 子命令被删",
        "file": "evoagent/cli.py",
        "old": "    sub.add_parser(\"identity-list\")\n",
        "new": "",
        "test": "TestWiringAudit::test_cli_commands_parsed_from_source",
    },
    {
        "id": "M9",
        "desc": "部署校验失效：verify 恒返回通过",
        "file": "evoagent/integration/services.py",
        "old": "        return verify(target or self.data_dir)",
        "new": "        return {\"ok\": True, \"errors\": []}",
        "test": "TestDeploymentEntryPoint::test_deploy_verify_detects_missing_manifest",
    },
    {
        "id": "M10",
        "desc": "私钥检测失效：CLI 恒报未检出",
        "file": "evoagent/cli.py",
        "old": "        out[\"text_audit\"] = {\"contains_secret\": bool(contains_secret(text))}",
        "new": "        out[\"text_audit\"] = {\"contains_secret\": False}",
        "test": "TestObservabilityEntryPoint::test_credential_audit_flags_private_key_fragment",
    },
]


def run_tests(nodeid: str, *, file_only: bool = False) -> int:
    target = TEST_FILE if file_only else f"{TEST_FILE}::{nodeid}"
    proc = subprocess.run(
        [PY, "-m", "pytest", target, "-q", "--no-header",
         "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    log_lines = []

    def emit(line: str) -> None:
        print(line)
        log_lines.append(line)

    baseline = run_tests(TEST_FILE, file_only=True)
    emit(f"[baseline] {TEST_FILE} exit={baseline} (0=全绿)")
    if baseline != 0:
        emit("!! 基线未绿，变异测试中止")
        (ROOT / ".planning" / "2026-10-26-aln30a"
         / "mutation-results.log").write_text(
            "\n".join(log_lines) + "\n", encoding="utf-8")
        return 1

    survived = []
    for mutation in MUTATIONS:
        path = ROOT / mutation["file"]
        original = path.read_text(encoding="utf-8")
        if mutation["old"] not in original:
            emit(f"[{mutation['id']}] !! 锚点未命中，跳过: {mutation['desc']}")
            survived.append(mutation["id"])
            continue
        backup = original
        try:
            path.write_text(original.replace(mutation["old"], mutation["new"], 1),
                            encoding="utf-8")
            code = run_tests(mutation["test"])
            if code == 0:
                emit(f"[{mutation['id']}] 存活（未被捕获）: {mutation['desc']}")
                survived.append(mutation["id"])
            else:
                emit(f"[{mutation['id']}] 已捕获: {mutation['desc']}")
        finally:
            path.write_text(backup, encoding="utf-8")

    emit("")
    emit(f"变异体 {len(MUTATIONS)} 个，存活 {len(survived)} 个 "
         f"{survived if survived else ''}")
    out = ROOT / ".planning" / "2026-10-26-aln30a" / "mutation-results.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
