"""ALN-25-A: 有来源的主动帮助——授权范围、近期整理与建议交付。

权威规格：
- EA-UX-008（chat/extracted/157_qa.md）§6 F04「主动整理近期工作与机会发现」
  流程与 §6.4 验收条件；
- EA-PRODUCT-007（chat/extracted/156_qa.md）UX-18/UX-19/UX-20。

复用已接受切片，不重写：
- OpportunityService（goals/opportunities.py）：证据化机会 discover/assess/ranked；
- GoalGovernor（goals/governance.py）：目标族报告与停止裁决；
- MemoryControlService（memory/user_control.py，ALN-24-D）：记忆作用域允许策略
  （memory_scope_policy 表，禁止优先）。

本服务是只读分析，不执行：
1. organize()——F04 第一~三步。显式授权范围（范围外来源不读取；记忆作用域
   过 24-D 策略，无策略服务时对记忆 fail-closed 并记录）；证据集合按
   user_stated / recorded / inferred / unconfirmed 四级分类，逐条带可解析
   引用；冲突、过期与待复核记忆及过时的非终态任务标记待确认，
   不把过时状态当作当前事实（UX-19）。被取代记忆经 supersede 置 revoked，
   由存储层 listing 排除（ALN-05 已接受切片），不进入本服务输出。
2. suggest()——F04 第四~五步。仅「已排序且效用超过门槛」的候选进入建议，
   无高价值允许不输出建议（UX-F04-02）；四段式交付
   （发现→为何关注→怎么做→权限/成本，UX-20）；每条建议显式记录
   「不等于执行授权」，本服务不提交任务、不登记目标、不产生外部副作用。

边界：app.py 装配、HTTP 端点与 App.tsx 接线属共享集成文件，不在本卡写入
边界，留保留缺口；UX-18 的「任务执行中实时发现缺口」属运行时主链，本卡
覆盖其可复用出口——终态任务衍生的增益候选经同一筛选与四段式交付。
"""

import json
import time

from ..memory.store import Credibility, Scope as MemoryScope
from .governance import GoalGovernor
from .opportunities import OpportunityService
from .store import TERMINAL, GoalStore, _number, _text

EVIDENCE_LEVELS = ("user_stated", "recorded", "inferred", "unconfirmed")
SECTIONS = ("completed", "in_progress", "blocked", "unresolved_issues",
            "confirmed_constraints", "time_nodes", "pending_confirmation")
STALE_TASK_SECONDS = 3600.0
DEADLINE_HORIZON_SECONDS = 7 * 86400.0
DEFAULT_MIN_SCORE = 0.0


class AssistScopeError(ValueError):
    """授权范围规格非法（未知字段/非字符串标识/未知记忆作用域/非法门槛）。"""


class ProactiveAssistService:
    """F04 主动帮助只读分析服务（ALN-25-A）。

    只读 goals/tasks/memory 与机会候选；不调用 controller、不 submit_task、
    不 register 机会目标。装配（app.py/API/App.tsx）见保留缺口。
    """

    def __init__(self, db, *, memory_control=None, clock=None):
        self.db = db
        self.goals = GoalStore(db, clock=clock)
        self.clock = self.goals.clock
        self.governor = GoalGovernor(db, clock=self.clock)
        self.opportunities = OpportunityService(db, clock=self.clock)
        self.memory_control = memory_control

    # ---- 授权范围（F04 第一步：确认分析范围）----

    def _scope_spec(self, scope):
        if not isinstance(scope, dict):
            raise AssistScopeError("授权范围必须为对象")
        unknown = set(scope) - {"goal_ids", "task_ids", "memory_scopes"}
        if unknown:
            raise AssistScopeError("未知范围字段: " + ", ".join(sorted(unknown)))
        spec = {}
        for key in ("goal_ids", "task_ids"):
            values = scope.get(key) or []
            if not isinstance(values, list) or any(
                    not isinstance(v, str) or not v.strip() for v in values):
                raise AssistScopeError(key + " 必须为非空字符串数组")
            spec[key] = sorted({v.strip() for v in values})
        scopes = scope.get("memory_scopes") or []
        if not isinstance(scopes, list) or any(
                not isinstance(v, str) or not v.strip() for v in scopes):
            raise AssistScopeError("memory_scopes 必须为非空字符串数组")
        valid = {item.value for item in MemoryScope}
        for value in scopes:
            if value.strip() not in valid:
                raise AssistScopeError("未知记忆作用域: " + value)
        spec["memory_scopes"] = sorted({v.strip() for v in scopes})
        return spec

    def _resolve(self, spec):
        """把显式范围解析为可读记录；未解析标识与拒绝作用域如实登记。"""
        goals, tasks, unresolved = [], [], set()
        ordered_tasks = []
        for goal_id in spec["goal_ids"]:
            try:
                goal = self.goals.get(goal_id)
            except KeyError:
                unresolved.add(("goal", goal_id))
                continue
            goals.append(goal)
            # 授权目标的任务族整体在范围内（复用 ALN-14 已接受的递归族查询）
            for task, _ in self.governor._tasks(goal_id):
                if task.task_id not in {t.task_id for t in ordered_tasks}:
                    ordered_tasks.append(task)
        for task_id in spec["task_ids"]:
            if task_id in {t.task_id for t in ordered_tasks}:
                continue
            task = self.db.get_task(task_id)
            if task is None:
                unresolved.add(("task", task_id))
            else:
                ordered_tasks.append(task)
        memory_allowed, memory_denied = [], []
        for scope in spec["memory_scopes"]:
            if self.memory_control is None:
                memory_denied.append({"scope": scope, "reason": "no_scope_policy_service"})
            elif not self.memory_control.scope_allowed(scope):
                memory_denied.append({"scope": scope, "reason": "denied_by_scope_policy"})
            else:
                memory_allowed.append(scope)
        scope_record = {
            "requested": spec,
            "resolved_goal_ids": [goal["goal_id"] for goal in goals],
            "resolved_task_ids": [task.task_id for task in ordered_tasks],
            "memory_scopes_allowed": memory_allowed,
            "memory_scopes_denied": memory_denied,
            "unresolved": [{"kind": kind, "id": ident, "reason": "not_found"}
                           for kind, ident in sorted(unresolved)],
        }
        return {"goals": goals, "tasks": ordered_tasks, "memory_allowed": memory_allowed,
                "memory_denied": memory_denied, "scope": scope_record}

    # ---- 近期整理（F04 第二/三步：建立证据集合、整理当前状态）----

    def organize(self, scope, *, actor, reason):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        spec = self._scope_spec(scope)
        resolved = self._resolve(spec)
        now = self.clock()
        entries = []
        sections = {name: [] for name in SECTIONS}

        def add(section, *, ref, summary, evidence, unconfirmed_reason=None, detail=None):
            if evidence not in EVIDENCE_LEVELS:
                raise AssertionError("unknown evidence level: " + str(evidence))
            entry = {"ref": ref, "summary": summary, "evidence": evidence, "section": section}
            if unconfirmed_reason:
                entry["unconfirmed_reason"] = unconfirmed_reason
            if detail:
                entry["detail"] = detail
            entries.append(entry)
            sections[section].append(entry)

        for goal in resolved["goals"]:
            self._collect_goal(goal, now, add)
        for task in resolved["tasks"]:
            self._collect_task(task, now, add)
        self._collect_memory(resolved, now, add)
        return {"ok": True, "scope": resolved["scope"], "sections": sections,
                "evidence": entries, "counts": {name: len(items) for name, items in sections.items()},
                "actor": actor, "reason": reason, "at": now}

    def _collect_goal(self, goal, now, add):
        gid, status = goal["goal_id"], goal["status"]
        ref = {"kind": "goal", "id": gid}
        title = goal["title"]
        summary = "目标「%s」状态 %s" % (title, status)
        if status == "completed":
            add("completed", ref=ref, summary=summary, evidence="recorded", detail={"status": status})
        elif status in ("proposed", "ready", "running", "waiting", "review"):
            add("in_progress", ref=ref, summary=summary, evidence="recorded", detail={"status": status})
        elif status == "blocked":
            add("blocked", ref=ref, summary=summary, evidence="recorded", detail={"status": status})
        elif status == "paused":
            add("blocked", ref=ref, summary=summary + "（治理暂停）", evidence="recorded",
                detail={"status": status})
        else:  # cancelled / expired 等终态而未完成
            add("unresolved_issues", ref=ref, summary=summary + "（未完成即终止）",
                evidence="recorded", detail={"status": status})
        try:
            report = self.governor.report(gid)
        except (KeyError, PermissionError):
            report = None
        if report and report.get("reason"):
            section = {"blocked": "blocked", "paused": "unresolved_issues"}.get(status, "in_progress")
            add(section, ref=ref,
                summary="治理裁决：%s（尝试 %d，无进展 %d，失败 %d）" % (
                    report["reason"], report["attempts"], report["no_progress"], report["failures"]),
                evidence="inferred",
                detail={"reason": report["reason"], "policy_ref": report["policy_ref"]})
        criteria = goal.get("success_criteria") or []
        if criteria:
            add("confirmed_constraints", ref=ref, summary="成功标准：%s" % "；".join(criteria),
                evidence="recorded", detail={"success_criteria": criteria})
        policy = self._goal_grant_policy(gid)
        if policy is not None:
            add("confirmed_constraints", ref=ref,
                summary="授权范围：tools=%s permissions=%s max_risk=%s" % (
                    policy.get("tools", []), policy.get("permissions", []),
                    policy.get("max_risk", "critical")),
                evidence="recorded", detail={"policy": policy})
        deadline = goal.get("deadline")
        if deadline and status not in TERMINAL:
            if deadline <= now:
                add("time_nodes", ref=ref, summary="期限已过期且未终态（%s）" % _stamp(deadline),
                    evidence="recorded", detail={"deadline": deadline})
            elif deadline - now <= DEADLINE_HORIZON_SECONDS:
                add("time_nodes", ref=ref,
                    summary="期限临近：%.1f 天内" % ((deadline - now) / 86400.0),
                    evidence="recorded", detail={"deadline": deadline})

    def _collect_task(self, task, now, add):
        ref = {"kind": "task", "id": task.task_id}
        status = task.task_status.value
        if task.is_terminal():
            if status == "SUCCEEDED":
                add("completed", ref=ref, summary="任务已完成：%s" % (task.user_goal or "")[:120],
                    evidence="recorded",
                    detail={"result": (task.result or "")[:200], "finished_at": task.updated_at})
            else:
                add("unresolved_issues", ref=ref,
                    summary="任务未成功（%s）：%s" % (status, (task.failure_reason or "无原因")[:120]),
                    evidence="recorded",
                    detail={"failure_reason": task.failure_reason or "", "finished_at": task.updated_at})
            return
        age = now - (task.updated_at or task.created_at or now)
        if age > STALE_TASK_SECONDS:
            # F04 第三步：不能把过时任务状态当作当前事实
            add("pending_confirmation", ref=ref,
                summary="任务状态 %s 已过时（%.0f 秒未更新），不当作当前事实" % (status, age),
                evidence="unconfirmed", unconfirmed_reason="stale_state",
                detail={"state": status, "age_seconds": age})
        else:
            add("in_progress", ref=ref, summary="任务进行中（%s）：%s" % (status, (task.user_goal or "")[:120]),
                evidence="recorded", detail={"state": status})

    def _collect_memory(self, resolved, now, add):
        for scope in resolved["memory_allowed"]:
            items = self.memory_control.store.list_items(scope=MemoryScope(scope), limit=200)
            for item in items:
                ref = {"kind": "memory", "id": item.memory_id}
                summary = "记忆（%s/%s，%s）：%s" % (item.kind.value, item.scope.value,
                                                   item.status.value, item.content[:120])
                detail = {"source": item.source, "status": item.status.value,
                          "kind": item.kind.value, "scope": item.scope.value}
                if item.has_conflict:
                    add("pending_confirmation", ref=ref, summary=summary + "（存在冲突）",
                        evidence="unconfirmed", unconfirmed_reason="conflict",
                        detail=dict(detail, conflict_ids=item.conflict_ids))
                elif item.is_expired(now):
                    add("pending_confirmation", ref=ref, summary=summary + "（已过期）",
                        evidence="unconfirmed", unconfirmed_reason="expired",
                        detail=dict(detail, valid_until=item.valid_until))
                elif item.status == Credibility.PENDING_REVIEW:
                    add("pending_confirmation", ref=ref, summary=summary + "（待复核）",
                        evidence="unconfirmed", unconfirmed_reason="pending_review", detail=detail)
                elif item.source == "user":
                    add("confirmed_constraints", ref=ref, summary=summary, evidence="user_stated",
                        detail=detail)
                elif item.status == Credibility.VERIFIED:
                    add("confirmed_constraints", ref=ref, summary=summary, evidence="recorded",
                        detail=detail)
                else:
                    add("pending_confirmation", ref=ref, summary=summary + "（未验证推断）",
                        evidence="inferred", unconfirmed_reason="candidate", detail=detail)

    # ---- 建议交付（F04 第四/五步：发现机会、筛选高价值、四段式交付）----

    def suggest(self, scope, *, actor, reason, min_score=DEFAULT_MIN_SCORE):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        _number(min_score, "min_score")
        spec = self._scope_spec(scope)
        resolved = self._resolve(spec)
        authorized = {"goal:" + goal["goal_id"] for goal in resolved["goals"]}
        authorized |= {"task:" + task.task_id for task in resolved["tasks"]}
        suggestions, suppressed = [], []
        excluded_out_of_scope = 0
        for candidate in self.opportunities.ranked():
            if candidate["source_ref"] not in authorized:
                excluded_out_of_scope += 1
                continue
            state = candidate["ranking_state"]
            if state == "expired":
                suppressed.append({"candidate_id": candidate["candidate_id"], "reason": "expired"})
            elif state != "ranked":
                suppressed.append({"candidate_id": candidate["candidate_id"], "reason": "needs_evidence"})
            elif candidate["score"] <= min_score:
                suppressed.append({"candidate_id": candidate["candidate_id"],
                                   "reason": "below_threshold", "score": candidate["score"]})
            else:
                suggestions.append(self._format_suggestion(candidate))
        return {"ok": True, "scope": resolved["scope"], "suggestions": suggestions,
                "suppressed": suppressed, "excluded_out_of_scope": excluded_out_of_scope,
                "withheld": None if suggestions else "no_high_value_opportunity",
                "actor": actor, "reason": reason, "at": self.clock()}

    def _format_suggestion(self, candidate):
        assessment = candidate["assessment"]
        kind, _, source_id = candidate["source_ref"].partition(":")
        return {
            "candidate_id": candidate["candidate_id"],
            "source_ref": candidate["source_ref"],
            "score": candidate["score"],
            "what": candidate["title"],
            "why": ("来源 %s:%s 的已评估机会：预期收益 %s，证据置信度 %s，估计成本 %s，风险 %s"
                    % (kind, source_id, assessment["expected_benefit"],
                       assessment["evidence_confidence"], assessment["estimated_cost"],
                       assessment["risk"])),
            "how": ("显式登记为机会目标（OpportunityService.register，需操作者与理由）后，"
                    "走既有目标授权/治理/推进链路；本建议不自动登记或执行"),
            "requires": {
                "permissions": self._source_permissions(kind, source_id),
                "estimated_cost_usd": assessment["estimated_cost"],
                "risk": assessment["risk"],
                "budget_check": "执行前须经预算预留与对账（ALN-15 原子预留）",
            },
            "evidence_refs": [{"kind": "opportunity", "id": candidate["candidate_id"]},
                              {"kind": kind, "id": source_id}],
            "authorization": {"executed": False, "auto_executed": False,
                              "note": "建议不等于用户授权执行建议；任何外部操作须另经权限、风险与预算检查"},
            "assessment": assessment,
            "expires_at": candidate["expires_at"],
        }

    # ---- 内部：授权 Grants 只读解析（建议的「权限」段）----

    def _goal_grant_policy(self, goal_id):
        try:
            goal = self.goals.get(goal_id)
        except KeyError:
            return None
        ref = goal.get("authorization_ref")
        if not ref:
            return None
        row = self.db._conn.execute("SELECT data, revoked_at FROM goal_grants WHERE authorization_ref=?",
                                    (ref,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        return json.loads(row["data"]).get("policy")

    def _source_permissions(self, kind, source_id):
        goal_id = source_id if kind == "goal" else None
        if goal_id is None:
            task = self.db.get_task(source_id)
            goal_id = task.goal_id if task is not None else None
        if goal_id is None:
            return {"resolved": False, "note": "未解析到来源目标；执行前须显式授权"}
        policy = self._goal_grant_policy(goal_id)
        if policy is None:
            return {"resolved": False, "goal_id": goal_id,
                    "note": "来源目标无有效授权 Grants；执行前须显式授权"}
        return {"resolved": True, "goal_id": goal_id, "tools": policy.get("tools", []),
                "permissions": policy.get("permissions", []),
                "max_risk": policy.get("max_risk", "critical")}


def _stamp(value):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
