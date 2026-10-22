"""REST 路由注册表（协作基座 F-00.2）。

**动机**：`api/server.py` 的 `do_GET`/`do_POST` 是 1500 行里的顺序
`if path == ...` 链（209 处 `self._json`）。任何特性要加端点都得改这个
共享文件 —— 并行开发时必然冲突，且改错一行会连带影响整条链。

**用法**：特性在自己的模块里注册路由，`server.py` 一行都不用改。

    from .routes import get, post

    @get("/things")
    def list_things(ctx):
        return ctx.json(200, {"things": ctx.app.db.list_things()})

    @post("/things")
    def create_thing(ctx):
        body = ctx.body
        ...
        return ctx.json(201, {...})

**分派约定**：`server.py::do_GET` 在鉴权之后、进入既有 `if` 链**之前**
先调用 `dispatch_get`。注册表未命中才走原有链路（fallback 保持不变），
因此存量端点行为零变化，新端点靠注册表生效。

**鉴权约定**：鉴权在 `server.py` 中统一完成（`_authorize`），
`ctx.auth.role` 已可用；注册表处理器不需要也不应该重复鉴权。
需要权限判断时用 `ctx.role_can_read(layer)`。
"""

from typing import Callable, Dict, List, NamedTuple, Optional

Handler = Callable[["RouteContext"], Optional[tuple]]


class RouteContext:
    """传给路由处理器的上下文。

    `body` 是**惰性**的：只有处理器真正访问 `ctx.body` 时才读请求体。

    这点很关键 —— 部分存量端点（如 `POST /diagnostics/import`）直接从
    `self.rfile` 读原始字节，若 `server.py` 在分派前就急切解析 body，
    流已被消费，处理器将读到空数据。因此读取时机必须由处理器决定。
    """

    __slots__ = ("app", "method", "path", "query", "auth",
                 "json", "text", "role_can_read", "_body_getter", "_body")

    def __init__(self, app, method: str, path: str, query, body_getter,
                 auth, json, text, role_can_read):
        self.app = app
        self.method = method
        self.path = path
        self.query = query
        self.auth = auth
        self.json = json
        self.text = text
        self.role_can_read = role_can_read
        self._body_getter = body_getter
        self._body = None

    @property
    def body(self) -> Dict[str, object]:
        if self._body is None:
            self._body = self._body_getter()
        return self._body


_GET: List[tuple] = []
_POST: List[tuple] = []
_PUT: List[tuple] = []


def get(path: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _GET.append((path, fn))
        return fn
    return deco


def post(path: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _POST.append((path, fn))
        return fn
    return deco


def put(path: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _PUT.append((path, fn))
        return fn
    return deco


def _match(routes: List[tuple], path: str):
    """返回首个匹配的处理器；无匹配返回 None。

    先精确匹配，再前缀匹配（`/tasks/` 这类），保持与既有链一致的语义。
    """
    for route_path, fn in routes:
        if route_path == path:
            return fn
    for route_path, fn in routes:
        if "{" not in route_path:
            continue
        pattern, actual = route_path.split("/"), path.split("/")
        if len(pattern) == len(actual) and all(
                bool(value) if part.startswith("{") and part.endswith("}")
                else part == value for part, value in zip(pattern, actual)):
            return fn
    prefix_hits = [(p, fn) for p, fn in routes if p.endswith("/") and
                   path.startswith(p)]
    if prefix_hits:
        # 同一前缀下最长的规则优先（更具体者胜）
        prefix_hits.sort(key=lambda pair: len(pair[0]), reverse=True)
        return prefix_hits[0][1]
    return None


def dispatch_get(ctx: RouteContext) -> bool:
    """命中并执行返回 True；未命中返回 False（调用方继续既有链路）。

    注意返回的是"是否命中"，不是处理器的返回值 —— 处理器约定是调用
    `ctx.json(...)` 直接写响应，其返回值为 None。
    """
    fn = _match(_GET, ctx.path)
    if fn is None:
        return False
    fn(ctx)
    return True


def dispatch_post(ctx: RouteContext) -> bool:
    fn = _match(_POST, ctx.path)
    if fn is None:
        return False
    fn(ctx)
    return True


def dispatch_put(ctx: RouteContext) -> bool:
    fn = _match(_PUT, ctx.path)
    if fn is None:
        return False
    fn(ctx)
    return True


def registered(method: str) -> List[str]:
    """列出已注册路径（测试与文档用）。"""
    table = {"GET": _GET, "POST": _POST, "PUT": _PUT}
    return [p for p, _ in table.get(method, [])]


def load_registered() -> None:
    """导入所有特性路由模块，触发装饰器注册。

    新增特性只需在这里加一行 import —— 这是特性与 server.py 之间
    唯一的耦合点，且是一次性的声明式登记，不产生逻辑冲突。
    """
    # 后续特性在此登记：
    #   from . import <feature_routes>  # noqa: F401
    return None
