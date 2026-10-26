"""ALN-30-A 装配收口测试。

覆盖三类断言：
1. 能力可达性审计：装配后零悬空；缺入口必须报错而非静默通过。
2. 四个零入口域（deployment / recovery / observability / knowledge）各从
   CLI 走通一条**真实路径**（真实落盘、真实恢复，非 mock）。
3. 点名的装配缺口闭合：IdentityRegistry 注入 ReleaseDelivery、主动帮助
   服务挂载；以及失败/拒绝路径（无部署清单拒绝恢复、缺入口报错）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from evoagent import cli
from evoagent.app import App
from evoagent.integration import (CAPABILITIES, Capability, WiringError,
                                  api_routes, assert_wiring_complete,
                                  audit_wiring, cli_commands)

NEW_CLI_COMMANDS = ("host-deploy", "host-restore", "credential-audit",
                    "knowledge-list", "assist-organize", "identity-list",
                    "wiring-audit")


def run_cli(argv, capsys, data_dir):
    """以隔离实例目录执行 CLI，返回 (退出文本, JSON 负载或 None)。"""
    import os
    old = os.environ.get("EVOAGENT_DATA")
    os.environ["EVOAGENT_DATA"] = str(data_dir)
    old_argv = sys.argv
    sys.argv = ["evoagent"] + list(argv)
    try:
        cli.main()
    finally:
        sys.argv = old_argv
        if old is None:
            os.environ.pop("EVOAGENT_DATA", None)
        else:
            os.environ["EVOAGENT_DATA"] = old
    out = capsys.readouterr().out.strip()
    try:
        return out, json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return out, None


@pytest.fixture()
def instance(tmp_path):
    """一个已部署（bootstrap）的实例目录——恢复路径要求部署清单存在。"""
    from evoagent.deployment.bootstrap import bootstrap
    target = tmp_path / "host"
    bootstrap(str(target), app_version="1.0.0", source_commit="aln30a")
    return target


# ---------------------------------------------------------------- 审计

class TestWiringAudit:
    def test_app_wiring_has_no_dangling_capability(self, tmp_path):
        app = App(data_dir=str(tmp_path / "inst"))
        report = audit_wiring(app)
        assert report["ok"] is True
        assert report["dangling"] == []
        assert report["wired"] == report["total"] == len(CAPABILITIES)

    def test_audit_detects_capability_without_any_entry(self, tmp_path):
        app = App(data_dir=str(tmp_path / "inst"))
        ghost = Capability(name="幽灵能力", domain="nowhere", card="ALN-99",
                           app_attr="not_a_real_attr", cli_command="no-such-cmd")
        report = audit_wiring(app, capabilities=[ghost])
        assert report["ok"] is False
        assert [d["capability"] for d in report["dangling"]] == ["幽灵能力"]

    def test_assert_raises_when_capability_dangling(self, tmp_path):
        app = App(data_dir=str(tmp_path / "inst"))
        ghost = Capability(name="幽灵能力", domain="nowhere", card="ALN-99")
        with pytest.raises(WiringError) as exc:
            assert_wiring_complete(app, capabilities=[ghost])
        assert "幽灵能力" in str(exc.value)

    def test_assert_passes_when_wiring_complete(self, tmp_path):
        app = App(data_dir=str(tmp_path / "inst"))
        report = assert_wiring_complete(app)
        assert report["ok"] is True

    def test_cli_commands_parsed_from_source(self):
        commands = cli_commands()
        for name in NEW_CLI_COMMANDS:
            assert name in commands

    def test_api_routes_enumerable(self):
        paths = api_routes()
        assert paths, "API 路由表不应为空"

    def test_capability_entries_record_source_card(self):
        for cap in CAPABILITIES:
            assert cap.card.startswith("ALN-")
            assert cap.domain
            assert cap.declared_entries()


# ---------------------------------------------------------------- app 装配

class TestAppWiring:
    def test_identity_registry_injected_into_delivery(self, tmp_path):
        app = App(data_dir=str(tmp_path / "inst"))
        assert app.identity_registry is not None
        assert app.delivery.identity_registry is app.identity_registry

    def test_zero_entry_domains_now_present(self, tmp_path):
        app = App(data_dir=str(tmp_path / "inst"))
        for attr in ("deployment", "recovery", "credentials", "knowledge",
                     "assist", "identity_registry"):
            assert hasattr(app, attr), attr

    def test_recovery_bound_to_instance_data_dir(self, tmp_path):
        data_dir = tmp_path / "inst"
        app = App(data_dir=str(data_dir))
        assert Path(app.recovery.data_dir) == data_dir

    def test_assist_service_uses_memory_control(self, tmp_path):
        app = App(data_dir=str(tmp_path / "inst"))
        assert app.assist.memory_control is app.memory_control

    def test_assembled_services_are_real_implementations(self, tmp_path):
        """装配的不是占位对象：每个新属性都是对应域的服务实例。

        针对变异 M4（删装配行）：仅靠审计"入口可达"判定会漏掉
        "app 上属性消失但 CLI 仍可达"的情形——这里直接钉住装配事实。
        """
        from evoagent.goals.assist import ProactiveAssistService
        from evoagent.integration.services import DeploymentService
        from evoagent.knowledge.store import KnowledgeStore
        from evoagent.memory.user_control import MemoryControlService
        from evoagent.observability.credentials import CredentialRegistry
        from evoagent.recovery.combined import CompositeRecovery
        from evoagent.release.identity import IdentityRegistry
        app = App(data_dir=str(tmp_path / "inst"))
        assert isinstance(app.deployment, DeploymentService)
        assert isinstance(app.recovery, CompositeRecovery)
        assert isinstance(app.credentials, CredentialRegistry)
        assert isinstance(app.knowledge, KnowledgeStore)
        assert isinstance(app.memory_control, MemoryControlService)
        assert isinstance(app.assist, ProactiveAssistService)
        assert isinstance(app.identity_registry, IdentityRegistry)

    def test_capability_registry_covers_all_wired_domains(self):
        """针对变异 M7（从注册表抹掉声明）：声明集必须覆盖六个能力名。

        注册表被削减时审计会"全绿"——所以声明集本身也要被钉住。
        """
        names = {cap.name for cap in CAPABILITIES}
        assert {"空机部署与版本固定", "组合恢复与 RTO/RPO",
                "凭据治理与泄漏拦截", "故障经验知识库",
                "有来源的主动帮助", "生产身份登记簿"} <= names
        domains = {cap.domain for cap in CAPABILITIES}
        assert {"deployment", "recovery", "observability", "knowledge",
                "goals", "release"} <= domains


# ---------------------------------------------------------------- CLI 真实路径

class TestDeploymentEntryPoint:
    def test_host_deploy_writes_real_manifest(self, tmp_path, capsys):
        target = tmp_path / "fresh-host"
        _, payload = run_cli(
            ["host-deploy", "--target", str(target), "--app-version", "2.1.0",
             "--source-commit", "abc123"], capsys, tmp_path / "inst")
        assert payload["manifest"]["app_version"] == "2.1.0"
        assert payload["manifest"]["source_commit"] == "abc123"
        assert payload["manifest"]["instance_id"]
        assert payload["verify"]["ok"] is True
        assert (target / "deployment.json").is_file()

    def test_host_deploy_rejects_non_empty_target(self, tmp_path, capsys):
        target = tmp_path / "dirty"
        target.mkdir()
        (target / "stray.txt").write_text("x", encoding="utf-8")
        _, payload = run_cli(["host-deploy", "--target", str(target)],
                             capsys, tmp_path / "inst")
        assert payload is not None
        assert "error" in payload

    def test_deploy_verify_detects_missing_manifest(self, tmp_path, capsys):
        """针对变异 M9（verify 恒过）：verify 必须真的在检查。

        bootstrap 的拒绝先于 verify 发生，故单靠 dirty 目录断言感知不到
        verify 被短路——这里对"已部署后又被删掉清单"的实例做校验，
        verify 被短路时该测试必然失败。
        """
        target = tmp_path / "deployed"
        _, _ = run_cli(["host-deploy", "--target", str(target),
                        "--app-version", "1.0.0"], capsys, tmp_path / "inst")
        assert (target / "deployment.json").is_file()
        (target / "deployment.json").unlink()
        app = App(data_dir=str(tmp_path / "inst"))
        check = app.deployment.verify(str(target))
        assert check["ok"] is False
        assert any("部署清单" in e for e in check["errors"])


class TestRecoveryEntryPoint:
    def test_host_restore_refuses_without_deployment_manifest(self, tmp_path,
                                                              capsys):
        """未部署的目录必须拒绝恢复——不得盲恢复。"""
        plain = tmp_path / "plain"
        plain.mkdir()
        # 先造出真实备份，让拒绝原因落在部署清单缺失而不是"备份不存在"
        run_cli(["backup", "create", "--name", "probe.zip"], capsys, plain)
        _, payload = run_cli(["host-restore", "probe.zip"], capsys, plain)
        assert payload is not None
        assert "error" in payload
        assert "部署" in payload["error"]

    def test_host_restore_completes_real_chain(self, tmp_path, capsys,
                                               instance):
        run_cli(["backup", "create", "--name", "aln30a-test.zip"],
                capsys, instance)
        _, payload = run_cli(["host-restore", "aln30a-test.zip"],
                             capsys, instance)
        assert payload["ok"] is True
        assert payload["identity_preserved"] is True
        assert payload["counts_matched"] is True
        assert payload["safety_backup"]


class TestObservabilityEntryPoint:
    def test_credential_audit_lists_registration(self, tmp_path, capsys):
        _, payload = run_cli(["credential-audit"], capsys, tmp_path / "inst")
        assert isinstance(payload["registered"], list)

    def test_credential_audit_flags_private_key_fragment(self, tmp_path,
                                                          capsys):
        secret = ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n"
                  "-----END PRIVATE KEY-----")
        _, payload = run_cli(["credential-audit", "--text", secret],
                             capsys, tmp_path / "inst")
        assert payload["text_audit"]["contains_secret"] is True

    def test_credential_audit_passes_clean_text(self, tmp_path, capsys):
        _, payload = run_cli(["credential-audit", "--text", "普通文本"],
                             capsys, tmp_path / "inst")
        assert payload["text_audit"]["contains_secret"] is False


class TestKnowledgeEntryPoint:
    def test_knowledge_list_returns_entries(self, tmp_path, capsys):
        _, payload = run_cli(["knowledge-list"], capsys, tmp_path / "inst")
        assert payload["count"] == 0
        assert payload["entries"] == []

    def test_knowledge_entries_visible_within_instance(self, tmp_path):
        """知识条目在实例内可见。

        knowledge 域是**进程内**存储（27-A 交付语义），跨实例/跨进程不共享——
        这条限制对 CLI 入口意味着：CLI 只能列出本进程登记的条目。
        """
        from evoagent.knowledge.store import KnowledgeEntry
        app = App(data_dir=str(tmp_path / "inst"))
        app.knowledge.add(KnowledgeEntry(
            entry_id="kn-1", key="provider-timeout", content="上游超时先重试一次",
            source="ALN-27-A", confidence=0.8,
            applicable_domains=("runtime",), version=1, created_at=1.0))
        assert len(app.knowledge.all()) == 1
        assert app.knowledge.all()[0].key == "provider-timeout"

    def test_knowledge_store_is_not_shared_across_instances(self, tmp_path):
        """跨实例不共享——进程内存储的既定语义，写入交付报告的保留缺口。"""
        from evoagent.knowledge.store import KnowledgeEntry
        first = App(data_dir=str(tmp_path / "inst"))
        first.knowledge.add(KnowledgeEntry(
            entry_id="kn-1", key="k", content="c", source="s", confidence=0.5,
            applicable_domains=("runtime",), version=1, created_at=1.0))
        second = App(data_dir=str(tmp_path / "inst"))
        assert len(second.knowledge.all()) == 0


class TestAssistEntryPoint:
    def test_assist_organize_returns_sections(self, tmp_path, capsys):
        _, payload = run_cli(["assist-organize"], capsys, tmp_path / "inst")
        assert payload["ok"] is True
        assert "sections" in payload
        assert payload["counts"] is not None


class TestIdentityEntryPoint:
    def test_identity_list_reports_registry(self, tmp_path, capsys):
        _, payload = run_cli(["identity-list"], capsys, tmp_path / "inst")
        assert payload["count"] == 0
        assert payload["active_key_id"] is None


class TestWiringAuditCommand:
    def test_wiring_audit_command_reports_zero_dangling(self, tmp_path, capsys):
        _, payload = run_cli(["wiring-audit"], capsys, tmp_path / "inst")
        assert payload["ok"] is True
        assert payload["dangling"] == []
        assert payload["wired"] == payload["total"]
