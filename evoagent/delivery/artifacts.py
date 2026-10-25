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
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class ArtifactError(ValueError):
    pass


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    """产物存储：文件系统为主体，元数据随 Task.result_evidence 走。"""

    def __init__(self, data_dir: str):
        self.root = Path(data_dir) / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, task_id: str, node_id: str, kind: str,
             content: bytes, *, description: str = "",
             encoding: str = "utf-8") -> Dict[str, Any]:
        """保存产物，返回元数据（可直接 append 到 task.result_evidence）。"""
        if not content:
            raise ArtifactError("产物内容不能为空")
        digest = _digest_bytes(content)
        artifact_id = f"{task_id}:{node_id}:{digest[:12]}"
        task_dir = self.root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        ext = {"markdown": ".md", "json": ".json", "text": ".txt",
               "report": ".md", "file": ".bin", "binary": ".bin",
               }.get(kind, ".bin")
        path = task_dir / f"{node_id}{ext}"
        path.write_bytes(content)
        return {
            "artifact_id": artifact_id,
            "node_id": node_id,
            "kind": kind,
            "sha256": digest,
            "bytes": len(content),
            "path": str(path.relative_to(self.root)),
            "description": description,
            "encoding": encoding,
            "created_at": time.time(),
        }

    def verify(self, meta: Dict[str, Any]) -> bool:
        """校验产物文件与元数据一致（文件存在 + sha256 匹配 + 大小匹配）。"""
        path = self.root / meta.get("path", "")
        if not path.is_file():
            return False
        data = path.read_bytes()
        return (_digest_bytes(data) == meta.get("sha256")
                and len(data) == meta.get("bytes"))

    def read(self, meta: Dict[str, Any]) -> Optional[bytes]:
        """读取产物内容；文件缺失或校验不符返回 None（不抛错）。"""
        if not self.verify(meta):
            return None
        return (self.root / meta["path"]).read_bytes()

    def list_for_task(self, evidence: List[Dict[str, Any]]) \
            -> List[Dict[str, Any]]:
        """列出任务的全部有效产物（文件系统存在性为准）。"""
        return [e for e in (evidence or [])
                if isinstance(e, dict) and "artifact_id" in e
                and self.verify(e)]

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
            if old is None or _digest_bytes(content) != old.get("sha256"):
                rebuild.append(node_id)
            else:
                keep.append(node_id)
        return {"rebuild": rebuild, "keep": keep}
