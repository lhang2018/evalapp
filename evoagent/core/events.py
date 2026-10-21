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
    # 时间来源：服务端事件为 server；客户端事件为 device（允许时钟偏差）
    clock_source: str = "server"
    severity: str = "info"                   # info | warn | error | critical
    correlation_id: Optional[str] = None     # 同一任务内相关性
    causation_id: Optional[str] = None       # 因果链
    payload: Dict[str, Any] = field(default_factory=dict)
    privacy_classification: str = "standard"  # standard | sensitive
    sync_status: str = "local"               # local | synced
    # 客户端事件在同一 run 内的单调序号；墙上时钟不能替代因果顺序
    sequence_no: Optional[int] = None
    # 产生事件时的不可变运行版本清单引用
    runtime_manifest_id: Optional[str] = None
    # 诊断附件引用（补充 C §6）；附件可先于事件完成或稍后补传
    attachment_refs: List[str] = field(default_factory=list)
    # 契约 §3：schema_version（GAP 第一批第5项·Event 字段批）
    schema_version: str = "1.0"
    # 契约 §3：payload_ref = local-uri-or-inline-small-payload。
    # 内联 payload 继续走 payload 字段；大附件/远端引用走这里。
    payload_ref: Optional[str] = None

    # 契约 §3 privacy_level 四值映射（core/contracts.py 裁定：spec 为准）。
    # 内部字段 privacy_classification 迁移期保留：证据层/追溯层的红去逻辑
    # 以其为准，不在此处改动行为口径。
    _PRIVACY_SPEC_MAP = {
        "standard": "internal",
        "sensitive": "sensitive",
        "secret": "restricted",
        "private": "restricted",
    }

    @property
    def privacy_level(self) -> str:
        """契约 §3 四值（public/internal/sensitive/restricted）。"""
        return self._PRIVACY_SPEC_MAP.get(
            self.privacy_classification, "restricted")

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
            "privacy_level": self.privacy_level,
            "sync_status": self.sync_status,
            "sequence_no": self.sequence_no,
            "runtime_manifest_id": self.runtime_manifest_id,
            "clock_source": self.clock_source,
            "attachment_refs": self.attachment_refs,
            "schema_version": self.schema_version,
            "payload_ref": self.payload_ref,
        }


_EXTRA_COLUMNS = {
    "component_version": "TEXT DEFAULT '0.1.0'",
    "correlation_id": "TEXT",
    "causation_id": "TEXT",
    "privacy_classification": "TEXT DEFAULT 'standard'",
    "sync_status": "TEXT DEFAULT 'local'",
    "sequence_no": "INTEGER",
    "runtime_manifest_id": "TEXT",
    "clock_source": "TEXT DEFAULT 'server'",
    "attachment_refs": "TEXT",
    "schema_version": "TEXT DEFAULT '1.0'",
    "payload_ref": "TEXT",
}

_EVENT_COLUMNS = (
    "event_id, task_id, run_id, severity, event_type, component_id, timestamp,"
    " payload, component_version, correlation_id, causation_id,"
    " privacy_classification, sync_status, sequence_no, runtime_manifest_id,"
    " clock_source, attachment_refs, schema_version, payload_ref")


def _row_to_event(row) -> RunEvent:
    evt = RunEvent(
        event_id=row[0], task_id=row[1], run_id=row[2], severity=row[3],
        event_type=row[4], component_id=row[5], timestamp=row[6],
        component_version=row[8] or "0.1.0",
        correlation_id=row[9], causation_id=row[10],
        privacy_classification=row[11] or "standard",
        sync_status=row[12] or "local",
        sequence_no=row[13], runtime_manifest_id=row[14],
        clock_source=row[15] or "server",
        schema_version=row[17] or "1.0",
        payload_ref=row[18])
    try:
        evt.payload = json.loads(row[7])
    except Exception:
        pass
    try:
        refs = json.loads(row[16]) if row[16] else []
        evt.attachment_refs = [str(r) for r in refs if isinstance(r, str)]
    except Exception:
        evt.attachment_refs = []
    return evt


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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """旧库补齐事件字段（PRD 40 / Arch 9.1），历史行取列默认值。"""
        cols = {r[1] for r in
                self._conn.execute("PRAGMA table_info(runtime_events)")}
        for name, ddl in _EXTRA_COLUMNS.items():
            if name not in cols:
                self._conn.execute(
                    f"ALTER TABLE runtime_events ADD COLUMN {name} {ddl}")

    def subscribe(self, fn: Callable[[RunEvent], None]) -> None:
        self._subs.append(fn)

    def _insert(self, evt: RunEvent, mode: str) -> bool:
        if self._conn is None:
            return True
        with self._lock:
            cur = self._conn.execute(
                f"INSERT {mode} INTO runtime_events ({_EVENT_COLUMNS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (evt.event_id, evt.task_id, evt.run_id, evt.severity,
                 evt.event_type, evt.component_id, evt.timestamp,
                 evt.json_payload(), evt.component_version,
                 evt.correlation_id, evt.causation_id,
                 evt.privacy_classification, evt.sync_status,
                 evt.sequence_no, evt.runtime_manifest_id,
                 evt.clock_source, json.dumps(evt.attachment_refs),
                 evt.schema_version, evt.payload_ref))
            self._conn.commit()
            return cur.rowcount > 0

    def emit(self, **kwargs: Any) -> RunEvent:
        evt = RunEvent(**{k: v for k, v in kwargs.items() if k in RunEvent.__dataclass_fields__})
        for fn in list(self._subs):
            try:
                fn(evt)
            except Exception:  # 订阅者异常不能破坏主流程
                pass
        self._insert(evt, "OR REPLACE")
        return evt

    def ingest(self, evt: RunEvent) -> bool:
        """接收客户端事件；event_id 已存在返回 False（幂等，不触发订阅者）。

        契约 §3 sync_status：入库即服务端确认（acknowledged）——
        回执由批次接口下发，事件自身状态以 acknowledged 落库
        （contracts.py CONFLICT_DECISIONS 裁定补齐 acknowledged）。
        存量 legacy "synced" 记录原样保留，读取兼容。
        """
        evt.sync_status = "acknowledged"
        return self._insert(evt, "OR IGNORE")

    def exists(self, event_id: str) -> bool:
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM runtime_events WHERE event_id=?", (event_id,)).fetchone()
        return row is not None

    def counts(self) -> Dict[str, Dict[str, int]]:
        """事件计数（PRD 37-40）：按类型/级别/同步状态聚合。"""
        if self._conn is None:
            return {"by_type": {}, "by_severity": {},
                    "by_sync_status": {}, "total": 0}
        by_type = {r[0]: int(r[1]) for r in self._conn.execute(
            "SELECT event_type, COUNT(*) FROM runtime_events"
            " GROUP BY event_type")}
        by_sev = {r[0]: int(r[1]) for r in self._conn.execute(
            "SELECT severity, COUNT(*) FROM runtime_events"
            " GROUP BY severity")}
        by_sync = {r[0]: int(r[1]) for r in self._conn.execute(
            "SELECT sync_status, COUNT(*) FROM runtime_events"
            " GROUP BY sync_status")}
        return {"by_type": by_type, "by_severity": by_sev,
                "by_sync_status": by_sync,
                "total": sum(by_type.values())}

    def count(self, task_id: Optional[str] = None) -> int:
        if self._conn is None:
            return 0
        if task_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM runtime_events WHERE task_id=?",
                (task_id,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM runtime_events").fetchone()
        return int(row[0]) if row else 0

    def delete_task_events(self, task_id: str) -> int:
        if self._conn is None:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM runtime_events WHERE task_id=?", (task_id,))
            self._conn.commit()
            return int(cur.rowcount)

    def purge_before(self, timestamp: float) -> int:
        if self._conn is None:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM runtime_events WHERE timestamp<?", (timestamp,))
            self._conn.commit()
            return int(cur.rowcount)

    def aggregate(self, limit: int = 10000) -> Dict[str, Any]:
        """可观测指标聚合（PRD 40 / Arch 20.1）。

        从结构化事件计算模型/工具/ReAct/安全指标；不把原始日志当结论。
        """
        out: Dict[str, Any] = {
            "tool_calls": {"total": 0, "ok": 0, "failed": 0,
                           "error_classes": {}, "contract_errors": 0,
                           "contract_error_rate": None, "by_tool": {}},
            "model_calls": {"total": 0, "ok": 0, "failed": 0,
                            "success_rate": None, "by_model": {}},
            "react": {"runs": 0, "anomalies": 0, "anomaly_rate": None},
            "context_warnings": 0,
            "security_incidents": 0,
        }
        if self._conn is None:
            return out
        rows = self._conn.execute(
            "SELECT event_type, severity, payload FROM runtime_events"
            " ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        anomaly_reasons = {"repeated_failure", "max_iter", "max_iterations",
                           "no_progress", "no_action"}
        for event_type, severity, raw in rows:
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001
                payload = {}
            if event_type == "tool_call":
                tc = out["tool_calls"]
                tc["total"] += 1
                ok = bool(payload.get("ok"))
                tool = str(payload.get("tool") or payload.get("tool_id")
                           or "unknown")
                bucket = tc["by_tool"].setdefault(
                    tool, {"total": 0, "ok": 0, "failed": 0,
                           "contract_errors": 0})
                bucket["total"] += 1
                if ok:
                    tc["ok"] += 1
                    bucket["ok"] += 1
                else:
                    tc["failed"] += 1
                    bucket["failed"] += 1
                    err = payload.get("error_class") or "unknown"
                    tc["error_classes"][err] = \
                        tc["error_classes"].get(err, 0) + 1
                    if "contract" in str(err) or "invalid" in str(err):
                        tc["contract_errors"] += 1
                        bucket["contract_errors"] += 1
            elif event_type == "model_call":
                mc = out["model_calls"]
                mc["total"] += 1
                ok = bool(payload.get("ok"))
                mc["ok" if ok else "failed"] += 1
                model = str(payload.get("model_id") or "unknown")
                bucket = mc["by_model"].setdefault(
                    model, {"total": 0, "ok": 0, "failed": 0,
                            "latency_s": 0.0})
                bucket["total"] += 1
                bucket["ok" if ok else "failed"] += 1
                try:
                    bucket["latency_s"] += float(
                        payload.get("latency_s") or 0.0)
                except (TypeError, ValueError):
                    pass
            elif event_type == "react_finished":
                rc = out["react"]
                rc["runs"] += 1
                if str(payload.get("stop_reason")) in anomaly_reasons:
                    rc["anomalies"] += 1
            elif event_type == "context_quality_warning":
                out["context_warnings"] += 1
            if severity == "critical" and any(
                    k in event_type for k in
                    ("safety", "security", "permission", "privacy")):
                out["security_incidents"] += 1

        tc = out["tool_calls"]
        if tc["total"]:
            tc["contract_error_rate"] = round(
                tc["contract_errors"] / tc["total"], 4)
        for bucket in tc["by_tool"].values():
            bucket["success_rate"] = round(
                bucket["ok"] / bucket["total"], 4) if bucket["total"] else None
        mc = out["model_calls"]
        if mc["total"]:
            mc["success_rate"] = round(mc["ok"] / mc["total"], 4)
        rc = out["react"]
        if rc["runs"]:
            rc["anomaly_rate"] = round(rc["anomalies"] / rc["runs"], 4)
        return out

    def query_range(self, start: Optional[float] = None,
                    end: Optional[float] = None,
                    limit: int = 20000) -> List[RunEvent]:
        """按时间窗升序读取事件，供漂移检测等窗口对比使用。"""
        if self._conn is None:
            return []
        sql = f"SELECT {_EVENT_COLUMNS} FROM runtime_events WHERE 1=1"
        args: list = []
        if start is not None:
            sql += " AND timestamp>=?"
            args.append(start)
        if end is not None:
            sql += " AND timestamp<?"
            args.append(end)
        sql += " ORDER BY timestamp ASC LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_event(r) for r in rows]

    def query(self, task_id: Optional[str] = None, limit: int = 100) -> List[RunEvent]:
        if self._conn is None:
            return []
        sql = f"SELECT {_EVENT_COLUMNS} FROM runtime_events"
        args: list = []
        if task_id:
            sql += " WHERE task_id=?"
            args.append(task_id)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        out = [_row_to_event(r) for r in rows]
        out.reverse()
        return out

    def query_run(self, run_id: str, limit: int = 20000) -> List[RunEvent]:
        """按 run 内 sequence_no 升序还原顺序（PRD 73 / Arch 9.1）。

        设备时钟可能有偏差，时间戳不能替代序号；历史无序号行排在有序
        事件之后，并退化为按时间排序。
        """
        if self._conn is None:
            return []
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM runtime_events WHERE run_id=?"
            " ORDER BY CASE WHEN sequence_no IS NULL THEN 1 ELSE 0 END,"
            " sequence_no ASC, timestamp ASC LIMIT ?",
            (run_id, limit)).fetchall()
        return [_row_to_event(r) for r in rows]
