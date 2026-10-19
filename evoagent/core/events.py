"""运行事件模型与事件总线（Architecture 9.1）。

事件是运行观测的基本单元；原始日志不能自动等同于记忆或进化结论。
"""

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class RunEvent:
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str = "info"
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    component_id: str = "system"
    component_version: str = "0.1.0"
    timestamp: float = field(default_factory=time.time)
    severity: str = "info"                   # info | warn | error | critical
    correlation_id: Optional[str] = None     # 同一任务内相关性
    causation_id: Optional[str] = None       # 因果链
    payload: Dict[str, Any] = field(default_factory=dict)
    privacy_classification: str = "standard"  # standard | sensitive
    sync_status: str = "local"               # local | synced

    def json_payload(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, default=str)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id, "event_type": self.event_type,
            "task_id": self.task_id, "run_id": self.run_id,
            "component_id": self.component_id, "component_version": self.component_version,
            "timestamp": self.timestamp, "severity": self.severity,
            "correlation_id": self.correlation_id, "causation_id": self.causation_id,
            "payload": self.payload,
            "privacy_classification": self.privacy_classification,
            "sync_status": self.sync_status,
        }


class EventBus:
    """进程内事件分发 + 可选 SQLite 落盘（运行事件分层中的『结构化运行事件』）。"""

    def __init__(self, db_path: Optional[str] = None):
        self._subs: List[Callable[[RunEvent], None]] = []
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS runtime_events ("
                "event_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, severity TEXT,"
                "event_type TEXT, component_id TEXT, timestamp REAL, payload TEXT)")
            self._conn.commit()

    def subscribe(self, fn: Callable[[RunEvent], None]) -> None:
        self._subs.append(fn)

    def emit(self, **kwargs: Any) -> RunEvent:
        evt = RunEvent(**{k: v for k, v in kwargs.items() if k in RunEvent.__dataclass_fields__})
        for fn in list(self._subs):
            try:
                fn(evt)
            except Exception:  # 订阅者异常不能破坏主流程
                pass
        if self._conn is not None:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO runtime_events VALUES (?,?,?,?,?,?,?,?)",
                    (evt.event_id, evt.task_id, evt.run_id, evt.severity,
                     evt.event_type, evt.component_id, evt.timestamp,
                     evt.json_payload()))
                self._conn.commit()
        return evt

    def query(self, task_id: Optional[str] = None, limit: int = 100) -> List[RunEvent]:
        if self._conn is None:
            return []
        sql = "SELECT event_id,task_id,run_id,severity,event_type,component_id,timestamp,payload FROM runtime_events"
        args: list = []
        if task_id:
            sql += " WHERE task_id=?"
            args.append(task_id)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            e = RunEvent(event_id=r[0], task_id=r[1], run_id=r[2], severity=r[3],
                         event_type=r[4], component_id=r[5], timestamp=r[6])
            try:
                e.payload = json.loads(r[7])
            except Exception:
                pass
            out.append(e)
        out.reverse()
        return out
