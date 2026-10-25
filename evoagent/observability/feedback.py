"""用户反馈存储（ALN-29-C）：绑定真实 task/run、脱敏、聚合、未验证不当根因。

- 凭据防线：文本先经 observability.credentials.scrub_text 脱敏；脱敏后仍
  检出秘密（contains_secret）则拒绝入库（CredentialLeakError 上抛）。
- 证据最小化：只保存 evidence_refs（事件/清单引用），不复制正文；
  读取正文沿用既有证据读权限，不在反馈里放大。
- 聚合：agg_key = category + 归一化文本 + task_id；重复反馈 count+1 并
  刷新 last_seen_at，不产生新行。
- 根因纪律：cause_status 恒为 "unverified"；只有诊断链路给出已验证结论
  才能改写（本模块不提供改写入口）。
"""

import hashlib
import json
import re
import sqlite3
import time
import threading
import uuid
from typing import Any, Dict, List, Optional

from .credentials import contains_secret, scrub_text

_CATEGORY = {"bug", "idea", "problem", "other"}


def _agg_key(category: str, text: str, task_id: Optional[str],
             run_id: Optional[str], manifest_id: Optional[str]) -> str:
    norm = " ".join(text.casefold().split())
    data = json.dumps([category, norm, task_id, run_id, manifest_id],
                      ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


class FeedbackError(ValueError):
    pass


class FeedbackStore:
    """用户反馈存储；conn 为 evoagent 主库连接（表由 schema 域自动建）。"""

    def __init__(self, conn: sqlite3.Connection, *, lock=None):
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()

    def submit(self, *, category: str, text: str,
               task_id: Optional[str] = None,
               run_id: Optional[str] = None,
               manifest_id: Optional[str] = None,
               evidence_refs: Optional[List[str]] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
        if not isinstance(category, str) or not isinstance(text, str):
            raise FeedbackError("反馈类型和内容必须是字符串")
        category = category.strip().lower()
        if category not in _CATEGORY:
            raise FeedbackError(f"未知反馈类型: {category or '(空)'}")
        text = (text or "").strip()
        if not text:
            raise FeedbackError("反馈内容不能为空")
        if len(text) > 4000:
            raise FeedbackError("反馈内容过长（≤4000 字）")
        scrubbed = scrub_text(text)
        if contains_secret(scrubbed) or contains_secret(text):
            raise FeedbackError(
                "反馈疑似包含凭据/秘密，已拒绝；请移除后重新提交")
        refs = [] if evidence_refs is None else evidence_refs
        if not isinstance(refs, list) or len(refs) > 32:
            raise FeedbackError("证据引用必须是列表且不超过32项")
        if any(not isinstance(r, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}", r)
                or contains_secret(r) for r in refs):
            raise FeedbackError("证据引用必须是有效标识且不得包含凭据")
        for identity in (task_id, run_id, manifest_id):
            if identity is not None and (not isinstance(identity, str)
                                         or contains_secret(identity)):
                raise FeedbackError("关联标识无效或包含凭据")
        now_ts = now if now is not None else time.time()
        key = _agg_key(category, scrubbed, task_id, run_id, manifest_id)
        fid = "fb_" + uuid.uuid4().hex[:16]
        with self._lock:
            if self._conn.in_transaction:
                raise FeedbackError("数据库已有在途事务，请稍后重新提交反馈")
            with self._conn:
                self._conn.execute(
                    "INSERT INTO user_feedback(feedback_id, agg_key, category, text,"
                    " task_id, run_id, manifest_id, evidence_refs, cause_status,"
                    " count, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)"
                    " ON CONFLICT(agg_key) DO UPDATE SET"
                    " count=user_feedback.count+1,last_seen_at=excluded.last_seen_at",
                    (fid, key, category, scrubbed, task_id, run_id, manifest_id,
                     json.dumps(refs), "unverified", now_ts, now_ts))
                row = self._conn.execute(
                    "SELECT feedback_id,count FROM user_feedback WHERE agg_key=?",
                    (key,)).fetchone()
            return {"feedback_id": row["feedback_id"],
                    "aggregated": row["feedback_id"] != fid, "count": row["count"]}

    def get(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM user_feedback WHERE feedback_id=?",
                (feedback_id,)).fetchone()
        return self._row(row) if row else None

    def list_recent(self, limit: int = 50,
                    task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if task_id:
                rows = self._conn.execute(
                    "SELECT * FROM user_feedback WHERE task_id=? "
                    "ORDER BY last_seen_at DESC LIMIT ?",
                    (task_id, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM user_feedback "
                    "ORDER BY last_seen_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        return {"feedback_id": row["feedback_id"],
                "category": row["category"],
                "text": row["text"],
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "manifest_id": row["manifest_id"],
                "evidence_refs": json.loads(row["evidence_refs"] or "[]"),
                "cause_status": row["cause_status"],
                "count": row["count"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"]}
