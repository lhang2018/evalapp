"""ALN-29-E：发布通道、更新控制与回滚体验测试。

复用 ReleaseManager 真实签名/校验/部署/回滚；本卡只验证通道与决定编排：
稳定/实验版本区分、实验需显式开启、坏包拒绝可见、无约束热切换禁止、
降级需回滚入口、真实回滚结果与目标版本绑定同一版本。
"""

import pytest

from evoagent.release.channels import (ChannelError, HOT_SWITCH_NOTE,
                                       ReleaseChannelService)
from evoagent.release.manager import ReleaseManager


@pytest.fixture
def release():
    manager = ReleaseManager(signing_secret="test-secret-1")
    stable_100 = manager.build_manifest(release_id="rel_100", version="v1.0.0",
                                        channel="stable")
    stable_110 = manager.build_manifest(release_id="rel_110", version="v1.1.0",
                                        channel="stable")
    experimental = manager.build_manifest(release_id="rel_200b",
                                          version="v2.0.0",
                                          channel="experimental")
    service = ReleaseChannelService(manager)
    return manager, service, stable_100, stable_110, experimental


def test_default_preference_is_stable(release):
    manager, service, *_ = release
    pref = service.preference("install-1")
    assert pref["channel"] == "stable"
    assert pref["allow_experimental"] is False
    assert pref["auto_update"] is True
    explanation = service.explain("install-1")
    assert explanation["channel"] == "stable"
    assert explanation["latest_rollback"] is None
    assert "稳定版本经过评测" in " ".join(explanation["notes"])


def test_experimental_channel_requires_explicit_opt_in(release):
    manager, service, *_ = release
    with pytest.raises(ChannelError):
        service.set_preference("install-1", channel="experimental",
                               actor="user", reason="试用实验版本")
    result = service.set_preference(
        "install-1", channel="experimental", allow_experimental=True,
        actor="user", reason="明确试用实验版本")
    assert result["ok"] is True
    assert result["preference"]["channel"] == "experimental"
    assert "实验通道" in " ".join(result["explanation"]["notes"])


def test_stable_install_refuses_experimental_release(release):
    manager, service, stable_100, stable_110, experimental = release
    result = service.evaluate_update("install-1", "rel_200b")
    assert result["ok"] is False
    assert result["decision"] == "refuse"
    assert any("实验版本" in reason for reason in result["reasons"])


def test_experimental_install_gets_install_decision_without_hot_switch(release):
    manager, service, stable_100, stable_110, experimental = release
    service.set_preference("install-1", channel="experimental",
                           allow_experimental=True, actor="user",
                           reason="opt-in")
    result = service.evaluate_update("install-1", "rel_200b")
    assert result["ok"] is True
    assert result["decision"] == "install"
    assert result["activation"]["requires_restart"] is True
    assert result["activation"]["must_rebind_release_identity"] is True
    assert result["activation"]["note"] == HOT_SWITCH_NOTE
    # 评估决定不改变当前运行版本（无约束热切换被禁止）
    assert manager.current_release_id is None


def test_retired_release_refused(release):
    manager, service, stable_100, stable_110, experimental = release
    manager.retire("rel_110", reason="退役", actor="op")
    result = service.evaluate_update("install-1", "rel_110")
    assert result["ok"] is False
    assert any("退役" in reason for reason in result["reasons"])


def test_downgrade_requires_rollback_entry(release):
    manager, service, stable_100, stable_110, experimental = release
    assert manager.deploy("rel_100", gate_passed=True)["ok"]
    assert manager.deploy("rel_110", gate_passed=True)["ok"]
    refused = service.evaluate_update("install-1", "rel_100")
    assert refused["ok"] is False
    assert any("降级" in reason for reason in refused["reasons"])
    allowed = service.evaluate_update("install-1", "rel_100", as_rollback=True)
    assert allowed["ok"] is True and allowed["decision"] == "rollback"


def test_bad_package_rejection_is_visible(release):
    manager, service, stable_100, stable_110, experimental = release
    stable_100.signature = "00" * 32   # 篡改签名，模拟坏包
    result = service.evaluate_update("install-1", "rel_100")
    assert result["ok"] is False
    assert any("坏包" in reason for reason in result["reasons"])
    assert result["verification"]["ok"] is False
    assert result["verification"]["errors"]


def test_pinned_release_blocks_other_versions(release):
    manager, service, stable_100, stable_110, experimental = release
    service.set_preference("install-1", channel="stable", actor="user",
                           reason="固定版本", pinned_release_id="rel_100")
    refused = service.evaluate_update("install-1", "rel_110")
    assert refused["ok"] is False
    assert any("已固定版本" in reason for reason in refused["reasons"])
    assert manager.deploy("rel_100", gate_passed=True)["ok"]
    active = service.evaluate_update("install-1", "rel_100")
    assert active["ok"] is True and active["decision"] == "already_active"
    assert active["activation"]["requires_restart"] is False


def test_rollback_returns_real_result_bound_to_same_version(release):
    manager, service, stable_100, stable_110, experimental = release
    assert manager.deploy("rel_100", gate_passed=True)["ok"]
    assert manager.deploy("rel_110", gate_passed=True)["ok"]
    result = service.rollback_with_evidence("install-1", reason="新版本失败",
                                            actor="op")
    assert result["ok"] is True
    evidence = result["evidence"]
    assert evidence["target"]["release_id"] == "rel_100"
    assert evidence["target"]["version"] == "v1.0.0"
    assert evidence["verification"]["ok"] is True
    assert evidence["same_version_binding"] is True
    assert manager.current_release_id == "rel_100"
    explanation = service.explain("install-1")
    assert explanation["latest_rollback"]["to"] == "rel_100"
    assert explanation["latest_rollback"]["verified"] is True
    history = service.history("install-1")
    assert history["decisions"][-1]["decision"] == "rollback"


def test_rollback_target_missing_reports_failed(release):
    manager, service, *_ = release
    result = service.rollback_with_evidence("install-1", reason="尝试回滚")
    assert result["ok"] is False
    assert result["reasons"]


def test_history_and_explanation_partition_channels(release):
    manager, service, stable_100, stable_110, experimental = release
    manager.deploy("rel_100", gate_passed=True)
    explanation = service.explain("install-1")
    stable_ids = {row["release_id"] for row in explanation["stable_versions"]}
    exp_ids = {row["release_id"] for row in explanation["experimental_versions"]}
    assert "rel_100" in stable_ids and "rel_200b" not in stable_ids
    assert "rel_200b" in exp_ids
    assert explanation["current"]["release_id"] == "rel_100"


def test_preference_persists_and_reason_required(release, tmp_path):
    manager, _, *_ = release
    state = tmp_path / "channels.json"
    first = ReleaseChannelService(manager, state_path=str(state))
    first.set_preference("install-9", channel="stable", actor="user",
                         reason="保持稳定")
    second = ReleaseChannelService(manager, state_path=str(state))
    assert second.preference("install-9")["reason"] == "保持稳定"
    with pytest.raises(ChannelError):
        second.set_preference("install-9", channel="stable", actor="",
                              reason="")
