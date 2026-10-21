"""建表语句按域拆分（协作基座 F-00.1）。

原先 30 张表的 `CREATE TABLE` 全部写死在 `persistence.py::Database._init_schema`
一个方法里，任何特性要加表都必须改那个共享文件 —— 并行开发时必然冲突。

本包把建表语句按域拆成独立模块，每个模块导出 `TABLES: list[str]`，
`persistence.py` 只负责遍历执行。

**新增特性加表 = 新建一个域模块，零处改动**（本包通过 `pkgutil`
自动发现 `*.py`，无需在别处登记，因此不存在"注册表忘了更新"或
"两人同时改注册行"的冲突）。这是本基座的核心目的。

约定：
- 每个域模块导出 `DOMAIN: str`（域标识）与 `TABLES: list[str]`。
- 语句必须幂等（`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`），
  因为每次连接都会重放全部语句。
- 需要改已有表结构时走 `migrations.py`，不要在 TABLES 里写 ALTER。
- 模块文件名以 `_` 开头视为私有（下划线模块不会被自动发现）。
"""

import importlib
import pkgutil
from types import ModuleType
from typing import List, Tuple

_PACKAGE = __name__


def _discover() -> List[ModuleType]:
    """自动发现本包内的域模块（按名称排序以保证执行顺序稳定）。"""
    found = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{_PACKAGE}.{info.name}")
        if hasattr(mod, "TABLES") and hasattr(mod, "DOMAIN"):
            found.append(mod)
    found.sort(key=lambda m: m.DOMAIN)
    return found


def domain_modules() -> List[ModuleType]:
    """返回全部域模块（每次调用重新发现，允许运行期新增）。"""
    return _discover()


def all_statements() -> List[str]:
    """返回全部建表/建索引语句（按域名排序）。"""
    out: List[str] = []
    for mod in domain_modules():
        out.extend(mod.TABLES)
    return out


def domain_names() -> List[str]:
    """返回已注册的域名清单（测试与文档用）。"""
    return [mod.DOMAIN for mod in domain_modules()]


def table_names() -> Tuple[str, ...]:
    """从建表语句中解析出全部表名（测试与文档用）。"""
    names = []
    for stmt in all_statements():
        head = " ".join(stmt.split())
        if head.upper().startswith("CREATE TABLE IF NOT EXISTS"):
            rest = head[len("CREATE TABLE IF NOT EXISTS"):].strip()
            names.append(rest.split("(")[0].strip())
    return tuple(names)