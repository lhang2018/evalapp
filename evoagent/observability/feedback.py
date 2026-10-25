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

import json
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from .credentials import contains_secret, scrub_text

_CATEGORY = {"bug", "idea", "problem", "other"}


def _agg_key(category: str, text: str, task_id: Optional[str]) -> str:
    norm = re.sub(r"\s+", "", text.lower())[:120]
    return f"{category}|{norm}|{task_id or ''}"


class FeedbackError(ValueError):
    pass


class FeedbackStore:
    """用户反馈存储；conn 为 evoagent 主库连接（表由 schema 域自动建）。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def submit(self, *, category: str, text: str,
               task_id: Optional[str] = None,
               run_id: Optional[str] = None,
               manifest_id: Optional[str] = None,
               evidence_refs: Optional[List[str]] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
        category = (category or "").strip().lower()
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
        refs = [r for r in (evidence_refs or []) if isinstance(r, str)
                and r.strip()]
        now_ts = now if now is not None else time.time()
        key = _agg_key(category, scrubbed, task_id)
        existing = self._conn.execute(
            "SELECT feedback_id, count FROM user_feedback WHERE agg_key=?",
            (key,)).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE user_feedback SET count=count+1, last_seen_at=? "
                "WHERE feedback_id=?", (now_ts, existing["feedback_id"]))
            self._conn.commit()
            return {"feedback_id": existing["feedback_id"],
                    "aggregated": True,
                    "count": existing["count"] + 1}
        fid = "fb_" + uuid.uuid4().hex[:16]
        self._conn.execute(
            "INSERT INTO user_feedback(feedback_id, agg_key, category, text,"
            " task_id, run_id, manifest_id, evidence_refs, cause_status,"
            " count, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
            (fid, key, category, scrubbed, task_id, run_id, manifest_id,
             json.dumps(refs), "unverified", now_ts, now_ts))
        self._conn.commit()
        return {"feedback_id": fid, "aggregated": False, "count": 1}

    def get(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM user_feedback WHERE feedback_id=?",
            (feedback_id,)).fetchone()
        return self._row(row) if row else None

    def list_recent(self, limit: int = 50,
                    task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
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
