# -*- coding: utf-8 -*-
"""ALN-18-H 缺口：编码包升级/重装与半写入回滚。

构造"升级失败"场景，断言消费者**保留原版本与注册表**（无半装条目）、
旧工具仍可执行、磁盘半写入如实上报；成功后原子替换。
"""

import os

import pytest

from evoagent.experts.coding_consumer import (
    CodingConsumerError,
    CodingConsumerRefused,
    CodingExpertConsumer,
)
from evoagent.tools.coding import CODING_PACK


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    consumer = CodingExpertConsumer()
    consumer.install(str(root), expected_version=CODING_PACK.version,
                     actor="tester", reason="初始安装")
    return consumer, str(root)


def test_reinstall_success_is_atomic(project):
    consumer, root = project
    result = consumer.reinstall(root, expected_version=CODING_PACK.version,
                                actor="tester", reason="升级到最新包")
    assert result["ok"] is True
    assert result["rolled_back"] is False
    assert result["previous_version"] == CODING_PACK.version
    assert result["version"] == CODING_PACK.version
    status = consumer.status(root)
    assert status["installed"] is True


def test_reinstall_failure_keeps_previous_install(project):
    consumer, root = project

    def failing(registry, project_root):
        with open(os.path.join(project_root, "half.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("y = 2\n")          # 模拟升级过程写到一半
        raise OSError("模拟升级半写入失败")

    result = consumer.reinstall(root, expected_version=CODING_PACK.version,
                                actor="tester", reason="模拟失败升级",
                                installer=failing)
    assert result["ok"] is False
    assert result["rolled_back"] is True
    assert result["restored_version"] == CODING_PACK.version
    # 半写入如实上报，不假装磁盘无损
    assert "half.py" in result["disk_changes"]["added"]
    # 旧注册表仍可用（原版本未被半装条目替换）
    recheck = consumer.execute(root, "coding_syntax_check", {"targets": ["."]},
                               actor="tester", reason="失败后复查")
    assert recheck["tool_id"] == "coding_syntax_check"
    assert consumer.status(root)["version"] == CODING_PACK.version


def test_reinstall_retry_after_failure_succeeds(project):
    consumer, root = project
    consumer.reinstall(root, expected_version=CODING_PACK.version,
                       actor="t", reason="先失败",
                       installer=lambda r, p: (_ for _ in ()).throw(OSError("boom")))
    retry = consumer.reinstall(root, expected_version=CODING_PACK.version,
                               actor="t", reason="重试成功")
    assert retry["ok"] is True
    assert retry["version"] == CODING_PACK.version


def test_reinstall_requires_installed_root(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    with pytest.raises(CodingConsumerError):
        CodingExpertConsumer().reinstall(str(root),
                                         expected_version=CODING_PACK.version,
                                         actor="t", reason="r")


def test_reinstall_rejects_wrong_version_and_missing_reason(project):
    consumer, root = project
    with pytest.raises(CodingConsumerRefused):
        consumer.reinstall(root, expected_version="0.0.0",
                           actor="t", reason="r")
    with pytest.raises(CodingConsumerError):
        consumer.reinstall(root, expected_version=CODING_PACK.version,
                           actor="t", reason="  ")
