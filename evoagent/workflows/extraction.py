"""从成功任务抽取工作流候选（PRD 13.4 / Arch 16.2）。

单次成功不构成可靠证据：同一意图下同一工具序列至少重复出现
`min_support` 次才生成候选。候选必须经结构校验、工具可用性检查与
人工批准后才能发布；发布前还需绑定工具必填参数（可直接执行）。

抽取只读取事件与任务记录，不重放任何工具调用。
"""

import hashlib
import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .library import save_workflow
from .models import WorkflowDef, WorkflowNode


class WorkflowExtractor:
    def __init__(self, db: Any, bus: Any, tools: Any,
                 audit: Any = None,
                 on_publish: Optional[Callable[[WorkflowDef], None]] = None,
                 min_support: int = 2):
        self.db = db
        self.bus = bus
        self.tools = tools
        self.audit = audit
        self.on_publish = on_publish
        self.min_support = max(2, int(min_support))

    # ---- 抽取 ----
    def extract(self, *, min_support: Optional[int] = None,
                limit: int = 200, actor: str = "operator") -> Dict[str, Any]:
        need = max(2, int(min_support or self.min_support))
        tasks = [t for t in self.db.list_tasks(limit=limit)
                 if t.task_status.value == "SUCCEEDED"
                 and (t.runtime_manifest or {}).get("source") != "evaluation"
                 and not t.workflow_ref]
        groups: Dict[tuple, List[Any]] = {}
        for task in tasks:
            seq = self._tool_sequence(task.task_id)
            if not seq:
                continue
            groups.setdefault((task.intent or "general", tuple(seq)),
                              []).append(task)

        existing = {(c.get("intent"), tuple(c.get("signature") or []))
                    for c in self.db.list_workflow_candidates(limit=1000)
                    if c.get("status") != "rejected"}
        created: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for (intent, seq), samples in sorted(
                groups.items(), key=lambda kv: (-len(kv[1]), kv[0][0])):
            if len(samples) < need:
                skipped.append({"intent": intent, "signature": list(seq),
                                "support": len(samples),
                                "reason": "insufficient_support"})
                continue
            if (intent, seq) in existing:
                skipped.append({"intent": intent, "signature": list(seq),
                                "support": len(samples),
                                "reason": "duplicate"})
                continue
            definition = self._build_definition(intent, list(seq), samples)
            now = time.time()
            cand = {
                "candidate_id": "wfc_" + uuid.uuid4().hex[:12],
                "intent": intent,
                "signature": list(seq),
                "support": len(samples),
                "sample_count": len(samples),
                "sample_tasks": [t.task_id for t in samples],
                "status": "candidate",
                "created_at": now,
                "updated_at": now,
                "definition": definition.to_dict(),
                "dynamic_inputs": self._required_inputs(seq),
            }
            self.db.save_workflow_candidate(cand)
            created.append(cand)
            self._audit(actor, "workflow.extract", cand["candidate_id"], {
                "intent": intent, "support": len(samples),
                "signature": list(seq)})
        return {"created": created, "skipped": skipped,
                "scanned": len(tasks), "min_support": need}

    def _tool_sequence(self, task_id: str) -> List[str]:
        events = self.bus.query(task_id=task_id, limit=500)
        seq = [e.payload.get("tool") for e in reversed(events)
               if e.event_type == "tool_call"
               and isinstance(e.payload, dict)
               and e.payload.get("ok")
               and e.payload.get("tool")]
        return seq

    def _build_definition(self, intent: str, seq: List[str],
                          samples: List[Any]) -> WorkflowDef:
        descriptions = self._descriptions(samples[-1])
        nodes: List[WorkflowNode] = []
        prev = ""
        for idx, tool_id in enumerate(seq, start=1):
            contract = self.tools.contract(tool_id)
            description = descriptions[idx - 1] if idx <= len(descriptions) \
                else ""
            node = WorkflowNode(
                node_id=f"step_{idx}",
                description=description or tool_id,
                deps=[prev] if prev else [],
                tool=tool_id,
                args={},
                max_retries=1,
                timeout=float(contract.timeout_policy) if contract else 30.0,
                requires_confirmation=bool(
                    contract and contract.side_effect_level != "none"),
                output_schema=dict(contract.output_schema) if contract else {},
            )
            nodes.append(node)
            prev = node.node_id
        digest = hashlib.sha256(
            json.dumps([intent, seq], ensure_ascii=False).encode()).hexdigest()
        latest = samples[-1]
        return WorkflowDef(
            workflow_id=f"wf_extracted_{digest[:10]}",
            version="0.1.0",
            name=f"自动抽取: {intent}",
            input_schema={"type": "object",
                          "properties": {"goal": {"type": "string"}}},
            nodes=nodes,
            acceptance_criteria=list(latest.acceptance_criteria),
            budget={"observed_tool_calls": self._observed_tool_calls(samples)},
            created_from_task=latest.task_id,
            status="candidate",
        )

    @staticmethod
    def _descriptions(task: Any) -> List[str]:
        plan = (task.runtime_manifest or {}).get("execution_plan") or {}
        return [str(s.get("description") or "")
                for s in plan.get("steps") or [] if s.get("tool_hint")]

    def _required_inputs(self, seq: List[str]) -> List[Dict[str, Any]]:
        out = []
        for idx, tool_id in enumerate(seq, start=1):
            contract = self.tools.contract(tool_id)
            required = list((contract.input_schema or {}).get("required") or [])
            if required:
                out.append({"node_id": f"step_{idx}", "tool": tool_id,
                            "required": required})
        return out

    # ---- 评测与决策 ----
    def evaluate(self, candidate_id: str,
                 args_by_step: Optional[Dict[str, Dict[str, Any]]] = None
                 ) -> Dict[str, Any]:
        cand = self._get(candidate_id)
        definition = WorkflowDef.from_dict(cand["definition"])
        problems = definition.validate()
        seq = list(cand.get("signature") or [])
        missing = [t for t in seq if self.tools.contract(t) is None]
        unbound = []
        overrides = args_by_step or {}
        for node in definition.nodes:
            contract = self.tools.contract(node.tool or "")
            required = list((contract.input_schema or {}).get("required") or []) \
                if contract else []
            provided = set(node.args or {}) | set(overrides.get(node.node_id) or {})
            lack = [name for name in required if name not in provided]
            if lack:
                unbound.append({"node_id": node.node_id, "tool": node.tool,
                                "missing": lack})
        structural = {
            "min_support": {"passed": int(cand["support"]) >= self.min_support,
                            "support": cand["support"],
                            "required": self.min_support},
            "structure": {"passed": not problems, "problems": problems},
            "tools_available": {"passed": not missing, "missing": missing},
        }
        passed = all(c["passed"] for c in structural.values())
        check = {
            "passed": passed,
            "runnable": passed and not unbound,
            "checks": structural,
            "input_binding": {"passed": not unbound, "unbound": unbound},
            "cost": {
                "planned_tool_calls": len(seq),
                "observed_avg_tool_calls": self._avg_tool_calls(
                    cand.get("sample_tasks") or []),
            },
            "evaluated_at": time.time(),
        }
        cand["evaluation"] = check
        cand["updated_at"] = time.time()
        self.db.save_workflow_candidate(cand)
        return check

    def approve(self, candidate_id: str, actor: str = "operator",
                reason: str = "") -> Dict[str, Any]:
        cand = self._get(candidate_id)
        if cand.get("status") != "candidate":
            raise ValueError(f"候选状态不允许批准: {cand.get('status')}")
        check = self.evaluate(candidate_id)
        if not check["passed"]:
            raise ValueError("评测未通过: " + self._fail_summary(check))
        return self._decide(cand, "approved", actor, reason,
                            "workflow.approve")

    def reject(self, candidate_id: str, actor: str = "operator",
               reason: str = "") -> Dict[str, Any]:
        cand = self._get(candidate_id)
        if cand.get("status") not in ("candidate", "approved"):
            raise ValueError(f"候选状态不允许拒绝: {cand.get('status')}")
        return self._decide(cand, "rejected", actor, reason,
                            "workflow.reject")

    def publish(self, candidate_id: str, actor: str = "operator",
                reason: str = "",
                args_by_step: Optional[Dict[str, Dict[str, Any]]] = None
                ) -> Dict[str, Any]:
        cand = self._get(candidate_id)
        if cand.get("status") != "approved":
            raise ValueError("必须先批准候选才能发布")
        check = self.evaluate(candidate_id, args_by_step)
        if not check["passed"]:
            raise ValueError("评测未通过: " + self._fail_summary(check))
        if not check["runnable"]:
            missing = check["input_binding"]["unbound"]
            raise ValueError("工具必填参数未绑定，不能发布: " + json.dumps(
                missing, ensure_ascii=False))
        definition = WorkflowDef.from_dict(cand["definition"])
        for node in definition.nodes:
            node.args = dict((args_by_step or {}).get(node.node_id) or {})
        definition.status = "published"
        problems = definition.validate()
        if problems:
            raise ValueError("定义非法: " + "; ".join(problems))
        save_workflow(self.db, definition, status="published")
        if self.on_publish is not None:
            self.on_publish(definition)
        cand["status"] = "published"
        cand["workflow_id"] = definition.workflow_id
        cand["version"] = definition.version
        cand["definition"] = definition.to_dict()
        cand["updated_at"] = time.time()
        cand["decision"] = {"actor": actor, "action": "publish",
                            "reason": reason, "at": cand["updated_at"]}
        self.db.save_workflow_candidate(cand)
        self._audit(actor, "workflow.publish", candidate_id, {
            "workflow_id": definition.workflow_id,
            "version": definition.version, "reason": reason,
            "args_bound": sorted((args_by_step or {}).keys())})
        return cand

    def list(self, status: Optional[str] = None,
             limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.list_workflow_candidates(status=status, limit=limit)

    def get(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get_workflow_candidate(candidate_id)

    # ---- 内部 ----
    def _get(self, candidate_id: str) -> Dict[str, Any]:
        cand = self.db.get_workflow_candidate(candidate_id)
        if cand is None:
            raise KeyError("工作流候选不存在")
        return cand

    def _decide(self, cand: Dict[str, Any], status: str, actor: str,
                reason: str, action: str) -> Dict[str, Any]:
        cand["status"] = status
        cand["updated_at"] = time.time()
        cand["decision"] = {"actor": actor, "action": status,
                            "reason": reason, "at": cand["updated_at"]}
        self.db.save_workflow_candidate(cand)
        self._audit(actor, action, cand["candidate_id"],
                    {"reason": reason, "status": status})
        return cand

    def _audit(self, actor: str, action: str, target: str,
               detail: Dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.record(actor, action, target=target, detail=detail)

    @staticmethod
    def _observed_tool_calls(samples: List[Any]) -> int:
        values = [WorkflowExtractor._usage(t).get("tool_calls", 0)
                  for t in samples]
        return max(values) if values else 0

    def _avg_tool_calls(self, task_ids: List[str]) -> Optional[float]:
        values = []
        for tid in task_ids:
            task = self.db.get_task(tid)
            if task is None:
                continue
            calls = self._usage(task).get("tool_calls")
            if isinstance(calls, (int, float)):
                values.append(float(calls))
        return round(sum(values) / len(values), 2) if values else None

    @staticmethod
    def _usage(task: Any) -> Dict[str, Any]:
        evidence = task.result_evidence or []
        if not evidence:
            return {}
        return ((evidence[-1] or {}).get("budget") or {}).get("usage") or {}

    @staticmethod
    def _fail_summary(check: Dict[str, Any]) -> str:
        failed = [name for name, c in (check.get("checks") or {}).items()
                  if not c.get("passed")]
        return ", ".join(failed) or "unknown"
