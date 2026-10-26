"""ALN-18-H：编程专家按项目安装与真实工具消费测试。

覆盖：受限项目根安装既有精确版本编码包并执行真实语法检查；同根幂等；
不同根独立注册表；跨根/链接/版本不符/未知工具拒绝；安装失败回滚并重新检查。
真实项目由用户选定后另行集成，本测试用临时目录固定测试依赖。
"""

import os

import pytest

from evoagent.experts import coding_consumer
from evoagent.experts.coding_consumer import (CodingConsumerError,
                                              CodingConsumerRefused,
                                              CodingExpertConsumer)
from evoagent.tools.coding import CODING_PACK


def project(tmp_path, *, main="print('ok')\n", test=None):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text(main, encoding="utf-8")
    if test is not None:
        (root / "test_main.py").write_text(test, encoding="utf-8")
    return str(root)


def test_install_and_run_real_syntax_check(tmp_path):
    root = project(tmp_path, main="def add(a, b):\n    return a + b\n",
                   test="from main import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    consumer = CodingExpertConsumer()
    installed = consumer.install(root, expected_version=CODING_PACK.version,
                                 actor="admin", reason="项目安装")
    assert installed["ok"] is True and installed["already_installed"] is False
    assert installed["version"] == CODING_PACK.version
    assert set(installed["tool_ids"]) == set(CODING_PACK.tool_ids)
    result = consumer.execute(root, "coding_syntax_check", {"targets": ["."]},
                              actor="admin", reason="语法检查")
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert result["duration_s"] >= 0
    assert isinstance(result["disk_changes"], dict)
    status = consumer.status(root)
    assert status["installed"] is True


def test_same_root_idempotent_and_after_install(tmp_path):
    root = project(tmp_path)
    consumer = CodingExpertConsumer()
    first = consumer.install(root, expected_version=CODING_PACK.version,
                             actor="admin", reason="首次")
    again = consumer.install(root, expected_version=CODING_PACK.version,
                             actor="admin", reason="重复")
    assert first["already_installed"] is False
    assert again["already_installed"] is True
    assert consumer.status()["count"] == 1


def test_two_roots_have_independent_registries(tmp_path):
    good = project(tmp_path, main="print('ok')\n")
    other_tmp = tmp_path / "b"
    other_tmp.mkdir()
    broken = project(other_tmp, main="def broken(:\n")
    consumer = CodingExpertConsumer()
    consumer.install(good, expected_version=CODING_PACK.version,
                     actor="admin", reason="根A")
    consumer.install(broken, expected_version=CODING_PACK.version,
                     actor="admin", reason="根B")
    ok = consumer.execute(good, "coding_syntax_check", {"targets": ["."]},
                          actor="admin", reason="A 检查")
    bad = consumer.execute(broken, "coding_syntax_check", {"targets": ["."]},
                           actor="admin", reason="B 检查")
    assert ok["ok"] is True and ok["returncode"] == 0
    assert bad["ok"] is False and bad["returncode"] != 0
    assert consumer.status()["count"] == 2


def test_refusals_cross_root_link_version_and_unknown_tool(tmp_path):
    root = project(tmp_path)
    consumer = CodingExpertConsumer()
    with pytest.raises(CodingConsumerError):
        consumer.execute(root, "coding_syntax_check", {"targets": ["."]},
                         actor="admin", reason="未安装")
    with pytest.raises(CodingConsumerRefused):
        consumer.install(root, expected_version="0.0.0-nope",
                         actor="admin", reason="版本不符")
    link = tmp_path / "link-proj"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        link = None  # 无符号链接权限的环境跳过该拒绝路径
    if link is not None:
        with pytest.raises(CodingConsumerRefused):
            consumer.install(str(link), expected_version=CODING_PACK.version,
                             actor="admin", reason="链接替换")
    consumer.install(root, expected_version=CODING_PACK.version,
                     actor="admin", reason="正常安装")
    with pytest.raises(CodingConsumerRefused):
        consumer.execute(root, "coding_unknown_tool", {},
                         actor="admin", reason="未知工具")


def test_install_failure_rolls_back_and_rechecks(tmp_path, monkeypatch):
    root = project(tmp_path)

    def broken_install(registry, project_root):
        install_coding_pack(registry, project_root) if False else None
        raise RuntimeError("注入的安装失败")

    monkeypatch.setattr(coding_consumer, "install_coding_pack", broken_install)
    consumer = CodingExpertConsumer()
    result = consumer.install(root, expected_version=CODING_PACK.version,
                              actor="admin", reason="失败回滚")
    assert result["ok"] is False
    assert result["rolled_back"] is True
    assert result["restored_version"] is None
    assert consumer.status()["count"] == 0
    assert result["disk_changes"] == {"added": [], "modified": [], "removed": []}
    # 回滚后重新安装正常版本并执行检查（恢复路径）
    monkeypatch.undo()
    recovered = consumer.install(root, expected_version=CODING_PACK.version,
                                 actor="admin", reason="恢复安装")
    assert recovered["ok"] is True
    check = consumer.execute(root, "coding_syntax_check", {"targets": ["."]},
                             actor="admin", reason="恢复后检查")
    assert check["ok"] is True


def test_app_wires_coding_consumer(tmp_path):
    from evoagent.app import App

    application = App(data_dir=str(tmp_path))
    try:
        assert isinstance(application.coding_consumer, CodingExpertConsumer)
        assert application.coding_consumer.status()["count"] == 0
    finally:
        application.db.close()
