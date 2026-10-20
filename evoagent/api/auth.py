"""API 鉴权与角色（PRD 83-85 / Architecture 18.1）。

未配置令牌时保持本地开发开放模式；配置后：
- operator 令牌可读并提交任务；
- admin 令牌可执行发布、回滚、候选审批等高风险动作。
令牌只从配置/环境读取，不写入日志与响应。
"""

from dataclasses import dataclass
from typing import Optional, Tuple

HIGH_RISK_POST = (
    "/releases/rollback",
)

ADMIN_ONLY_ACTIONS = {"approve", "deploy", "monitor", "rollback"}


@dataclass
class AuthResult:
    ok: bool
    role: str = "anonymous"
    status: int = 200
    error: str = ""


class ApiAuth:
    def __init__(self, token: Optional[str] = None,
                 admin_token: Optional[str] = None):
        self.token = token or None
        self.admin_token = admin_token or None

    @property
    def enabled(self) -> bool:
        return bool(self.token or self.admin_token)

    def check(self, authorization: Optional[str], method: str,
              path: str) -> AuthResult:
        if not self.enabled:
            return AuthResult(True, "admin")
        raw = (authorization or "").strip()
        presented = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
        if not presented:
            return AuthResult(False, "anonymous", 401, "缺少 Bearer 令牌")
        role = ""
        if self.admin_token and presented == self.admin_token:
            role = "admin"
        elif self.token and presented == self.token:
            role = "operator"
        if not role:
            return AuthResult(False, "anonymous", 403, "令牌无效")
        if method != "POST":
            return AuthResult(True, role)
        if path in HIGH_RISK_POST:
            if role != "admin":
                return AuthResult(False, role, 403, "该操作需要 admin 权限")
            return AuthResult(True, role)
        parts = [p for p in path.split("/") if p]
        if (len(parts) == 4 and parts[0] == "evolution"
                and parts[1] == "candidates"
                and parts[3] in ADMIN_ONLY_ACTIONS
                and role != "admin"):
            return AuthResult(False, role, 403, "该操作需要 admin 权限")
        return AuthResult(True, role)
