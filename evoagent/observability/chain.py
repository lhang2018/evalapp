"""决策链重建（ALN-16）。

按 task/run/effect/approval/诊断/评测/发布引用聚合事件与审计记录，
输出可复现的节点、边和缺失引用；不以原始日志直接充当结论。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .contract import (EventRefs, EvidenceEvent, as_evidence_event,
                       canonical_digest)

CHAIN_SCHEMA = "evoagent.observability.chain/1"


@dataclass(frozen=True)
class ChainNode:
    node_id: str
    kind: str
    event_id: str
    timestamp: float
    sequence_no: Optional[int]
    summary: str
    privacy_level: str
    trust: str
    untrusted_origin: str
    refs: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "sequence_no": self.sequence_no,
            "summary": self.summary,
            "privacy_level": self.privacy_level,
            "trust": self.trust,
            "untrusted_origin": self.untrusted_origin,
            "refs": dict(self.refs),
        }


@dataclass(frozen=True)
class ChainEdge:
    source: str
    target: str
    relation: str

    def to_dict(self) -> Dict[str, str]:
        return {"source": self.source, "target": self.target,
                "relation": self.relation}


@dataclass
class DecisionChain:
    task_id: str
    run_id: str
    nodes: Tuple[ChainNode, ...] = ()
    edges: Tuple[ChainEdge, ...] = ()
    missing_refs: Tuple[Dict[str, str], ...] = ()
    schema_version: str = CHAIN_SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "missing_refs": [dict(item) for item in self.missing_refs],
        }

    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def _select(self, **criteria: str) -> List[ChainNode]:
        out = []
        for node in self.nodes:
            if all(node.refs.get(key) == value
                   for key, value in criteria.items()):
                out.append(node)
        return out

    def by_effect(self, effect_id: str) -> List[ChainNode]:
        return self._select(effect_id=effect_id)

    def by_approval(self, approval_id: str) -> List[ChainNode]:
        return self._select(approval_id=approval_id)

    def by_node_run(self, node_run_id: str) -> List[ChainNode]:
        return self._select(node_run_id=node_run_id)

    def by_release(self, release_id: str) -> List[ChainNode]:
        return self._select(release_id=release_id)

    def summary(self) -> Dict[str, Any]:
        kinds: Dict[str, int] = {}
        privacy: Dict[str, int] = {}
        untrusted = 0
        for node in self.nodes:
            kinds[node.kind] = kinds.get(node.kind, 0) + 1
            privacy[node.privacy_level] = privacy.get(node.privacy_level, 0) + 1
            if node.trust == "untrusted":
                untrusted += 1
        return {"nodes": len(self.nodes), "by_kind": kinds,
                "by_privacy": privacy, "untrusted_nodes": untrusted,
                "edges": len(self.edges), "missing_refs": len(self.missing_refs)}


def _event_node(event: EvidenceEvent) -> ChainNode:
    refs = event.refs.to_dict()
    summary = event.event_type
    if event.fault_level != "none":
        summary += f":{event.fault_level}"
    return ChainNode(
        node_id=event.event_id, kind="event", event_id=event.event_id,
        timestamp=event.timestamp, sequence_no=event.sequence_no,
        summary=summary, privacy_level=event.privacy_level,
        trust=event.trust, untrusted_origin=event.untrusted_origin,
        refs=refs)


def _record_node(record: Dict[str, Any], kind: str,
                 id_field: str) -> ChainNode:
    identity = str(record.get(id_field) or record.get("id") or "")
    if not identity:
        identity = f"{kind}_missing"
    refs = {name: str(record.get(name) or "")
            for name in EventRefs.FIELDS
            if record.get(name)}
    if kind == "audit":
        refs.setdefault("audit_id", identity)
    elif kind == "approval":
        refs.setdefault("approval_id", identity)
    elif kind == "diagnostic":
        refs.setdefault("diagnostic_id", identity)
    elif kind == "evaluation":
        refs.setdefault("candidate_id", identity)
    elif kind == "release":
        refs.setdefault("release_id", identity)
    elif kind == "effect":
        refs.setdefault("effect_id", identity)
    summary_parts = [kind]
    for key in ("status", "action", "outcome", "result"):
        if record.get(key):
            summary_parts.append(str(record[key]))
            break
    return ChainNode(
        node_id=identity, kind=kind, event_id=identity,
        timestamp=float(record.get("timestamp")
                        or record.get("created_at") or 0.0),
        sequence_no=None, summary=":".join(summary_parts),
        privacy_level=str(record.get("privacy_level") or "internal"),
        trust=str(record.get("trust") or "trusted"),
        untrusted_origin=str(record.get("untrusted_origin") or ""),
        refs=refs)


def _sort_key(node: ChainNode) -> Tuple[Any, ...]:
    return (node.sequence_no is None,
            node.sequence_no if node.sequence_no is not None else 0,
            node.timestamp, node.node_id)


def build_decision_chain(
    events: Iterable[Any],
    *,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    audits: Iterable[Dict[str, Any]] = (),
    approvals: Iterable[Dict[str, Any]] = (),
    diagnostics: Iterable[Dict[str, Any]] = (),
    evaluations: Iterable[Dict[str, Any]] = (),
    releases: Iterable[Dict[str, Any]] = (),
    effects: Iterable[Dict[str, Any]] = (),
) -> DecisionChain:
    normalized = [as_evidence_event(item) for item in events]
    if task_id:
        normalized = [item for item in normalized
                      if item.refs.task_id == task_id]
    if run_id:
        normalized = [item for item in normalized
                      if item.refs.run_id == run_id]

    normalized.sort(key=lambda event: _sort_key(_event_node(event)))
    nodes: List[ChainNode] = [_event_node(item) for item in normalized]
    collections = {
        "audit": (audits, "audit_id"),
        "approval": (approvals, "approval_id"),
        "diagnostic": (diagnostics, "case_id"),
        "evaluation": (evaluations, "candidate_id"),
        "release": (releases, "release_id"),
        "effect": (effects, "effect_id"),
    }
    pending = []
    for kind, (records, id_field) in collections.items():
        for record in records:
            record = dict(record)
            if task_id and record.get("task_id") not in (None, "", task_id):
                continue
            if run_id and record.get("run_id") not in (None, "", run_id):
                continue
            node = _record_node(record, kind, id_field)
            if (not task_id or record.get("task_id") == task_id) and (
                    not run_id or record.get("run_id") == run_id):
                nodes.append(node)
            else:
                pending.append(node)
    # Global release/evaluation records may lack a task scope. Only include
    # them when a selected event or scoped record actually references them.
    while pending:
        refs = {(key, value) for node in nodes for key, value in node.refs.items()}
        linked = [node for node in pending if any(
            (key, value) in refs for key, value in node.refs.items()
            if key not in ("task_id", "run_id"))]
        if not linked:
            break
        nodes.extend(linked)
        pending = [node for node in pending if node not in linked]

    nodes.sort(key=_sort_key)
    by_event = {node.event_id: node for node in nodes}

    edges: List[ChainEdge] = []
    for event in normalized:
        if event.causation_id and event.causation_id in by_event:
            edges.append(ChainEdge(event.causation_id, event.event_id,
                                   "caused_by"))
        if event.refs.approval_id:
            source = _find(by_event, "approval", event.refs.approval_id)
            if source is not None:
                edges.append(ChainEdge(source.node_id, event.event_id,
                                       "approved_by"))
        if event.refs.release_id:
            source = _find(by_event, "release", event.refs.release_id)
            if source is not None:
                edges.append(ChainEdge(source.node_id, event.event_id,
                                       "released_in"))

    evaluated: Dict[str, str] = {}
    for node in nodes:
        if node.kind == "evaluation":
            candidate = node.refs.get("candidate_id")
            release = node.refs.get("release_id")
            if candidate:
                evaluated[candidate] = node.node_id
            if release:
                source = _find(by_event, "release", release)
                if source is not None:
                    edges.append(ChainEdge(node.node_id, source.node_id,
                                           "evaluates"))
    for node in nodes:
        if node.kind == "release":
            candidate = node.refs.get("candidate_id")
            if candidate and candidate in evaluated:
                edges.append(ChainEdge(evaluated[candidate], node.node_id,
                                       "promoted_to"))

    present = {
        "approval": {node.refs.get("approval_id") for node in nodes
                     if node.kind == "approval"},
        "diagnostic": {node.refs.get("diagnostic_id") for node in nodes
                       if node.kind == "diagnostic"},
        "candidate": {node.refs.get("candidate_id") for node in nodes
                      if node.kind in ("evaluation", "release")
                      and node.refs.get("candidate_id")},
        "release": {node.refs.get("release_id") for node in nodes
                    if node.kind == "release"},
        "effect": {node.refs.get("effect_id") for node in nodes
                   if node.kind == "effect"},
        "node_run": {node.refs.get("node_run_id") for node in nodes
                     if node.refs.get("node_run_id")},
    }
    present["candidate"].update({
        node.refs.get("candidate_id") for node in nodes
        if node.kind in ("evaluation", "release")})

    referenced_by: Dict[Tuple[str, str], set] = {}
    for event in normalized:
        mapping = {
            "approval": event.refs.approval_id,
            "diagnostic": event.refs.diagnostic_id,
            "candidate": event.refs.candidate_id,
            "release": event.refs.release_id,
            "effect": event.refs.effect_id,
            "node_run": event.refs.node_run_id,
        }
        for kind, identity in mapping.items():
            if identity:
                referenced_by.setdefault((kind, identity), set()).add(
                    event.event_id)

    missing: List[Dict[str, str]] = []
    for (kind, identity), sources in sorted(referenced_by.items()):
        if identity in present.get(kind, set()):
            continue
        missing.append({"kind": kind, "id": identity,
                        "referenced_by": ",".join(sorted(sources))})
    for event in normalized:
        if event.causation_id and event.causation_id not in by_event:
            missing.append({"kind": "event", "id": event.causation_id,
                            "referenced_by": event.event_id})
    edges = sorted(set(edges), key=lambda edge: (edge.source, edge.target, edge.relation))
    missing.sort(key=lambda item: (item["kind"], item["id"], item["referenced_by"]))

    task = task_id or (normalized[0].refs.task_id if normalized else "")
    run = run_id or (normalized[0].refs.run_id if normalized else "")
    return DecisionChain(task_id=task, run_id=run, nodes=tuple(nodes),
                         edges=tuple(edges), missing_refs=tuple(missing))


def _find(by_event: Dict[str, ChainNode], kind: str,
          identity: str) -> Optional[ChainNode]:
    for node in by_event.values():
        if node.kind == kind and identity in (node.node_id,
                                              node.refs.get(kind + "_id")):
            return node
    return None
