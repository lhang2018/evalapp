"""产物交付层（ALN-24-A）：真实文件产物的标识、版本、校验与访问。

复用 Task.result / result_evidence（既有权威），不建第二套产物权威：
- 每个产物有唯一 artifact_id（task_id + node_id + digest 前缀）；
- sha256 内容校验 + 字节大小 + kind + 描述；
- 落盘到 data_dir/artifacts/<task_id>/，文件名含 digest 防篡改；
- 局部修改：变更只重做受影响产物（其余产物 digest 不变即不变）。

边界：
- 产物正文只写本地文件系统，不进 KV/数据库（数据库只存元数据）；
- 预览/下载路径给 API 层使用（本模块不实现 HTTP）；
- "无文件不显示下载"：产物存在性以文件系统为准，不以元数据为准。
"""

import hashlib
import os
import re
import tempfile
import json
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional


class ArtifactError(ValueError):
    pass


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    """产物存储：文件系统为主体，元数据随 Task.result_evidence 走。"""

    def __init__(self, data_dir: str):
        self.root = (Path(data_dir) / "artifacts").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _component(value: str) -> str:
        if (not isinstance(value, str) or not value or value in (".", "..")
                or value.rstrip(". ") != value
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)):
            raise ArtifactError("产物任务/节点标识不能包含路径或非法文件名")
        return value

    def _path(self, relative: str, task_id: Optional[str] = None) -> Optional[Path]:
        if not isinstance(relative, str) or not relative or PureWindowsPath(relative).drive:
            return None
        raw = Path(relative.replace("\\", "/"))
        if raw.is_absolute():
            return None
        try:
            path = (self.root / raw).resolve()
            scope = self.root if task_id is None else self.root / self._component(task_id)
            if not path.is_relative_to(scope) or not path.is_relative_to(self.root):
                return None
            return path
        except (OSError, RuntimeError, ValueError):
            return None

    def save(self, task_id: str, node_id: str, kind: str,
             content: bytes, *, description: str = "",
             encoding: str = "utf-8") -> Dict[str, Any]:
        task_id, node_id = self._component(task_id), self._component(node_id)
        if not isinstance(content, bytes) or not content:
            raise ArtifactError("产物内容必须是非空字节串")
        digest = _digest_bytes(content)
        artifact_id = f"{task_id}:{node_id}:{digest}"
        task_dir = self._path(task_id)
        if task_dir is None:
            raise ArtifactError("产物路径超出存储根目录")
        task_dir.mkdir(parents=True, exist_ok=True)
        ext = {"markdown": ".md", "json": ".json", "text": ".txt",
               "report": ".md", "file": ".bin", "binary": ".bin"}.get(kind, ".bin")
        relative = f"{task_id}/{node_id}-{digest}{ext}"
        path = self._path(relative, task_id)
        if path is None:
            raise ArtifactError("产物路径超出存储根目录")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=task_dir, prefix=".artifact-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return {"artifact_id": artifact_id, "node_id": node_id, "kind": kind,
                "sha256": digest, "bytes": len(content), "path": relative,
                "description": description, "encoding": encoding,
                "created_at": time.time()}

    def read(self, meta: Dict[str, Any], *, limit: Optional[int] = None,
             task_id: Optional[str] = None) -> Optional[bytes]:
        """Read once, validate those exact bytes, and optionally retain only a preview."""
        if not isinstance(meta, dict):
            return None
        path = self._path(meta.get("path"), task_id)
        size, digest = meta.get("bytes"), meta.get("sha256")
        if (path is None or type(size) is not int or size < 0
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            return None
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ArtifactError("预览字节上限无效")
        chunks, total, kept, hasher = [], 0, 0, hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size:
                        return None
                    hasher.update(chunk)
                    piece = chunk if limit is None else chunk[:max(0, limit - kept)]
                    if piece:
                        chunks.append(piece)
                        kept += len(piece)
        except OSError:
            return None
        if total != size or hasher.hexdigest() != digest:
            return None
        return b"".join(chunks)

    def verify(self, meta: Dict[str, Any], *, task_id: Optional[str] = None) -> bool:
        return self.read(meta, limit=0, task_id=task_id) is not None

    def list_for_task(self, evidence: List[Dict[str, Any]], *,
                      task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [e for e in (evidence or []) if isinstance(e, dict)
                and "artifact_id" in e and self.verify(e, task_id=task_id)]

    def diff_for_rebuild(self, old_evidence: List[Dict[str, Any]],
                         new_content: Dict[str, bytes]) -> Dict[str, Any]:
        """局部修改：对比新旧内容，只重做 digest 变化的产物。

        返回 {"rebuild": [...要重做的node_id...],
              "keep": [...digest不变保留的node_id...]}。
        """
        old_by_node = {e.get("node_id"): e for e in (old_evidence or [])
                       if isinstance(e, dict) and "node_id" in e}
        rebuild, keep = [], []
        for node_id, content in (new_content or {}).items():
            old = old_by_node.get(node_id)
            if old is None or _digest_bytes(content) != old.get("sha256") or not self.verify(old):
                rebuild.append(node_id)
            else:
                keep.append(node_id)
        return {"rebuild": rebuild, "keep": keep}
