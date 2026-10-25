"""隔离候选宿主（ALN-09-H：隔离宿主执行真实候选资产）。

候选变更不能以任意代码字符串直接执行：必须先按精确引用
（``asset_id@version``）从资产库读取**真实候选资产**，校验内容摘要，再
写入一次性沙箱目录执行。执行面复用 ``evolution/sandbox.py``：独立子进程、
超时强杀、网络/子进程阻断、凭据不继承、输出截断。

契约（本卡新增的执行面约定）：

- 资产正文形如 ``{"entry": "main.py", "files": {"main.py": "...", ...}}``；
  没有 ``files`` 的资产被拒绝执行（类型不匹配的资产不能被猜成代码）。
- 执行前重新计算 ``content_digest(content)`` 并与资产登记摘要比对；
  不一致直接拒绝（资产库读取侧本身也会校验，这里是执行前的第二道）。
- 返回体同时包含**候选身份**（asset_id/version/content_digest/entry）与
  执行证据（status/exit_code/duration/violations/stdout/stderr），
  便于队列在崩溃恢复后核对「要执行的是哪一个候选」。

边界（不宣称超出能力）：这是降低风险的受控执行环境，不是针对恶意代码的
强安全边界（Python 审计钩子盖不住解释器之外的行为）。
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..assets.content import content_digest
from ..assets.entity import AssetError
from .sandbox import (DEFAULT_OUTPUT_LIMIT, DEFAULT_TIMEOUT_S, SandboxRunner)

DEFAULT_ENTRY = "candidate.py"


class CandidateHostError(RuntimeError):
    """候选宿主错误基类。"""


class CandidateAssetError(CandidateHostError):
    """候选资产不可执行（缺失/类型不符/摘要不符/路径非法）。"""


@dataclass(frozen=True)
class CandidateArtifact:
    """已校验的候选资产：身份 + 待执行文件集合。"""

    asset_id: str
    version: str
    content_digest: str
    entry: str
    files: Dict[str, str]

    @property
    def ref(self) -> str:
        return f"{self.asset_id}@{self.version}"

    def identity(self) -> Dict[str, Any]:
        return {"asset_ref": self.ref, "asset_id": self.asset_id,
                "version": self.version,
                "content_digest": self.content_digest,
                "entry": self.entry, "file_count": len(self.files)}


class IsolatedCandidateHost:
    """从资产库取真实候选资产，在一次性沙箱里执行。"""

    def __init__(self, repository: Any, sandbox: Optional[SandboxRunner] = None):
        self.repository = repository
        self.sandbox = sandbox or SandboxRunner()

    # ---- 取资产 + 校验身份 ----
    def resolve(self, ref: Any) -> CandidateArtifact:
        """按精确引用（``AssetRef`` 或 ``asset_id@version`` 字符串）读取资产。"""
        target = ref if isinstance(ref, str) else str(ref)
        try:
            asset = self.repository.get_exact(target)
        except AssetError as exc:  # AssetNotFound / AssetValidationError（摘要不符）
            raise CandidateAssetError(f"候选资产不可用: {exc}") from exc
        content = dict(getattr(asset, "content", None) or {})
        files = content.get("files")
        entry = content.get("entry") or DEFAULT_ENTRY
        if not isinstance(files, dict) or not files:
            raise CandidateAssetError(
                "候选资产缺少 content.files；只有代码型候选资产可被执行")
        for path, text in files.items():
            if not isinstance(path, str) or not path or not isinstance(text, str):
                raise CandidateAssetError(
                    "候选资产 content.files 必须是非空字符串到源码文本的映射")
        if not isinstance(entry, str) or entry not in files:
            raise CandidateAssetError(
                f"候选资产入口 {entry!r} 不在 content.files 中")
        actual = content_digest(content)
        declared = str(getattr(asset, "content_digest", "") or "")
        if actual != declared:
            raise CandidateAssetError(
                "候选资产正文与声明 content_digest 不一致，拒绝执行")
        return CandidateArtifact(asset_id=asset.asset_id, version=asset.version,
                                 content_digest=declared, entry=entry,
                                 files=dict(files))

    # ---- 执行 ----
    def run(self, ref: str, *, timeout: float = DEFAULT_TIMEOUT_S,
            output_limit: int = DEFAULT_OUTPUT_LIMIT) -> Dict[str, Any]:
        """执行精确引用指向的候选资产，返回候选身份 + 执行证据。"""
        artifact = self.resolve(ref)
        try:
            result = self.sandbox.run_project(
                artifact.files, entry=artifact.entry,
                timeout=timeout, output_limit=output_limit)
        except FileNotFoundError as exc:  # 入口文件在写盘阶段被发现缺失
            raise CandidateAssetError(str(exc)) from exc
        payload = artifact.identity()
        payload.update({"status": result.status,
                        "exit_code": result.exit_code,
                        "duration_s": result.duration_s,
                        "violations": list(result.violations),
                        "stdout": result.stdout,
                        "stderr": result.stderr})
        return payload
