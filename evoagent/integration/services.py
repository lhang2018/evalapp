"""运行主链装配用服务包装（ALN-30-A）。

``deployment`` 域交付的是模块级函数（``bootstrap`` / ``load`` / ``verify``），
没有可直接挂到 ``App`` 上的对象。这里提供一个薄包装，使部署能力在运行
主链上成为可调用入口（而不是停在库层的裸函数）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..deployment.bootstrap import bootstrap, load, verify

__all__ = ["DeploymentService", "list_identities"]


def list_identities(registry: Any) -> List[Dict[str, Any]]:
    """列举身份登记簿中的身份（只读）。

    ``release/identity.py``（ALN-03-P/11-P 交付边界）未提供公开列举接口，
    此处用受限 getattr 读取并集中在一处：取不到就返回空列表，不抛错、
    不猜字段。登记簿中只存公钥记录，导出无害。
    """
    items = getattr(registry, "_identities", None)
    if not isinstance(items, dict):
        return []
    return [dict(vars(identity)) for identity in items.values()]


class DeploymentService:
    """空机部署与版本固定的运行主链入口。

    ``data_dir`` 是默认部署目标；``bootstrap`` 面向"空机"场景，可在另一目录
    上初始化可运行实例，因此允许显式指定 ``target``。
    """

    def __init__(self, data_dir: str):
        self.data_dir = str(data_dir)

    def bootstrap(self, target: str = "", *, app_version: str = "",
                  source_commit: str = "",
                  allow_existing: bool = False) -> Dict[str, Any]:
        """在目标目录初始化可运行实例，返回部署清单。"""
        return bootstrap(target or self.data_dir, app_version=app_version,
                         source_commit=source_commit,
                         allow_existing=allow_existing)

    def load(self, target: str = "") -> Dict[str, Any]:
        """读取既有部署清单。"""
        return load(target or self.data_dir)

    def verify(self, target: str = "") -> Dict[str, Any]:
        """校验部署结构与版本固定状态。"""
        return verify(target or self.data_dir)
