"""Goal lifecycle and versioned autonomy grants (ALN-13).

Authority includes every ancestor, checked before each new model/tool action.
Budget refs identify immutable per-task hard limits; aggregate reservation and
opportunity scheduling are ALN-15/14 consumers, not implicit grants here.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from ..core.authorization import AuthorizationStore, RISK, _inside, check_policy, normalize_policy
from ..core.budget import BudgetLimits
from ..core.contracts import GOAL_STATUS_SPEC, GOAL_TYPE
from ..core.leases import assert_connection_lease
from ..core.task import Task

LEVELS = ("suggestion", "preparation", "sandbox", "execution", "continuous")
ACTION_LEVEL = {"suggest": 0, "prepare": 1, "sandbox": 2, "execute": 3, "advance": 4}
TERMINAL = {"completed", "cancelled", "expired"}
ACTIVE = {"ready", "running"}
TRANSITIONS = {
    "proposed": {"ready", "blocked", "cancelled", "expired"},
    "ready": {"running", "paused", "blocked", "cancelled", "expired"},
    "running": {"waiting", "paused", "blocked", "review", "cancelled", "expired"},
    "waiting": {"ready", "paused", "blocked", "cancelled", "expired"},
    "paused": {"ready", "blocked", "cancelled", "expired"},
    "blocked": {"ready", "paused", "cancelled", "expired"},
    "review": {"ready", "completed", "paused", "blocked", "cancelled", "expired"},
    "completed": set(), "cancelled": set(), "expired": set(),
}
ID = re.compile(r"[A-Za-z0-9_.:-]{4,64}\Z")


class GoalConflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + "不能为空")
    return value.strip()


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + "必须为有限数值")
    if value < 0 or (positive and value == 0):
        raise ValueError(label + "超出允许范围")
    return value


def _budget(policy):
    base = asdict(BudgetLimits())
    if not isinstance(policy, dict) or set(policy) - set(base):
        raise ValueError("未知任务预算字段")
    base.update(deepcopy(policy))
    if base.pop("tool_limits"):
        raise ValueError("Goal预算暂不接受tool_limits；使用逐任务数值上限")
    for key, value in base.items():
        _number(value, key)
        if key in {"max_model_calls", "max_tool_calls", "max_concurrent", "max_tool_calls_per_tool"} and not isinstance(value, int):
            raise ValueError(key + "必须为整数")
    return base


def _narrow(child, parent):
    for key in ("tools", "permissions"):
        if set(child[key]) - set(parent[key]):
            raise PermissionError("子目标扩大父目标" + key)
    if RISK[child.get("max_risk", "critical")] > RISK[parent.get("max_risk", "critical")]:
        raise PermissionError("子目标扩大风险范围")
    for key in ("targets", "device_ids", "path_roots"):
        if key not in parent:
            continue
        if key not in child:
            raise PermissionError("子目标未保留父目标边界: " + key)
        if key == "path_roots":
            allowed = all(any(_inside(Path(value), Path(root)) for root in parent[key]) for value in child[key])
        else:
            allowed = not set(child[key]) - set(parent[key])
        if not allowed:
            raise PermissionError("子目标扩大资源范围: " + key)


def _intersection(left, right):
    result = {key: sorted(set(left[key]) & set(right[key])) for key in ("tools", "permissions")}
    result["max_risk"] = min((left.get("max_risk", "critical"), right.get("max_risk", "critical")), key=RISK.get)
    for key in ("targets", "device_ids", "path_roots"):
        if key not in left and key not in right:
            continue
        if key not in left or key not in right:
            result[key] = deepcopy(left[key] if key in left else right[key])
        elif key != "path_roots":
            result[key] = sorted(set(left[key]) & set(right[key]))
        else:
            result[key] = sorted({a if _inside(Path(a), Path(b)) else b
                for a in left[key] for b in right[key]
                if _inside(Path(a), Path(b)) or _inside(Path(b), Path(a))})
    return result


class GoalStore:
    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or time.time

    @contextmanager
    def _write(self):
        with self.db._lock:
            self.db._conn.execute("BEGIN IMMEDIATE")
            try:
                assert_connection_lease(self.db._conn, self.db)
                yield
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise

    def _get(self, goal_id):
        row = self.db._conn.execute("SELECT data FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
        if row is None:
            raise KeyError("目标不存在: " + str(goal_id))
        return json.loads(row[0])

    def _lineage(self, goal_id):
        goals, seen = [], set()
        while goal_id:
            if goal_id in seen:
                raise PermissionError("目标引用形成环")
            seen.add(goal_id)
            goal = self._get(goal_id)
            goals.append(goal)
            goal_id = goal["parent_goal_id"]
        return goals

    def _save(self, goal, actor, reason, event):
        goal["updated_at"] = self.clock()
        self.db._conn.execute("""INSERT INTO goals VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(goal_id) DO UPDATE SET parent_goal_id=excluded.parent_goal_id,
            owner=excluded.owner,status=excluded.status,revision=excluded.revision,
            deadline=excluded.deadline,data=excluded.data""",
            (goal["goal_id"], goal["parent_goal_id"], goal["owner"], goal["status"],
             goal["revision"], goal["deadline"], _json(goal)))
        self.db._conn.execute("INSERT INTO goal_evidence VALUES (?,?,?,?,?,?,?)",
            (goal["goal_id"], goal["revision"], event, actor, reason, self.clock(), _json(goal)))
        return deepcopy(goal)

    def _version(self, goal, expected_revision):
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("必须提供expected_revision整数")
        if goal["revision"] != expected_revision:
            raise GoalConflict("目标版本已变更，请刷新")

    def _command(self, scope, command_id, payload):
        if command_id is None:
            return None, None
        if not isinstance(command_id, str) or not ID.fullmatch(command_id):
            raise ValueError("非法command_id")
        fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        row = self.db._conn.execute("SELECT * FROM goal_commands WHERE scope=? AND command_id=?",
                                    (scope, command_id)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise GoalConflict("幂等键与原命令不匹配")
            return json.loads(row["result"]), fingerprint
        return None, fingerprint

    def _remember(self, scope, command_id, fingerprint, result):
        if command_id is not None:
            self.db._conn.execute("INSERT INTO goal_commands VALUES (?,?,?,?)",
                                  (scope, command_id, fingerprint, _json(result)))
        return result

    def create(self, title, *, actor, owner=None, goal_type="explicit",
               success_criteria=None, parent_goal_id=None, deadline=None,
               goal_id=None, command_id=None):
        title, actor = _text(title, "目标"), _text(actor, "actor")
        owner = _text(owner or actor, "owner")
        if goal_type not in GOAL_TYPE:
            raise ValueError("非法goal_type")
        if parent_goal_id is not None and (not isinstance(parent_goal_id, str) or not ID.fullmatch(parent_goal_id)):
            raise ValueError("非法parent_goal_id")
        if goal_type == "derived" and not parent_goal_id:
            raise ValueError("派生目标必须关联父目标")
        if not isinstance(success_criteria, list) or not success_criteria or any(
                not isinstance(item, str) or not item.strip() for item in success_criteria):
            raise ValueError("success_criteria必须是非空字符串数组")
        if goal_id is not None and (not isinstance(goal_id, str) or not ID.fullmatch(goal_id)):
            raise ValueError("非法goal_id")
        if deadline is not None:
            _number(deadline, "deadline", positive=True)
        payload = dict(title=title, actor=actor, owner=owner, goal_type=goal_type,
                       success_criteria=success_criteria, parent_goal_id=parent_goal_id,
                       deadline=deadline, goal_id=goal_id)
        with self._write():
            cached, fingerprint = self._command("create", command_id, payload)
            if cached is not None:
                return cached
            if parent_goal_id:
                parent = self._get(parent_goal_id)
                if parent["status"] in TERMINAL:
                    raise PermissionError("父目标已终止")
                deadline = parent["deadline"] if deadline is None else deadline
                if parent["deadline"] is not None and (deadline is None or deadline > parent["deadline"]):
                    raise PermissionError("子目标期限超过父目标")
            identity = goal_id or "goal_" + uuid.uuid4().hex
            if self.db._conn.execute("SELECT 1 FROM goals WHERE goal_id=?", (identity,)).fetchone():
                raise GoalConflict("目标已存在")
            goal = dict(goal_id=identity, title=title, owner=owner, goal_type=goal_type,
                        parent_goal_id=parent_goal_id, success_criteria=success_criteria,
                        authorization_ref=None, budget_ref=None, deadline=deadline,
                        no_progress_policy_ref=None, status="proposed", evidence_refs=[],
                        revision=1, created_at=self.clock(), updated_at=self.clock())
            result = self._save(goal, actor, "register", "goal_created")
            return self._remember("create", command_id, fingerprint, result)

    def get(self, goal_id):
        self.expire_due()
        with self.db._lock:
            return self._get(goal_id)

    def list(self, *, owner=None, limit=100):
        self.expire_due()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit必须为1..1000")
        with self.db._lock:
            sql, params = "SELECT data FROM goals", []
            if owner is not None:
                sql += " WHERE owner=?"
                params.append(owner)
            return [json.loads(row[0]) for row in self.db._conn.execute(
                sql + " ORDER BY goal_id LIMIT ?", params + [limit])]

    def _grants(self, goal_id, action, *, active=True):
        if action not in ACTION_LEVEL:
            raise ValueError("未知自主动作")
        lineage, grants = self._lineage(goal_id), []
        now = self.clock()
        for goal in lineage:
            if goal["status"] in TERMINAL or (active and goal["status"] not in ACTIVE):
                raise PermissionError("目标或父目标尚未就绪、已暂停或已终止")
            if goal["deadline"] is None or goal["deadline"] <= now:
                raise PermissionError("目标缺少有效期限或已经过期")
            row = self.db._conn.execute("SELECT * FROM goal_grants WHERE authorization_ref=?",
                                        (goal["authorization_ref"],)).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise PermissionError("目标未经授权或授权已撤销")
            grant = json.loads(row["data"])
            if grant["expires_at"] <= now or grant["goal_id"] != goal["goal_id"]:
                raise PermissionError("目标授权失效")
            if LEVELS.index(grant["level"]) < ACTION_LEVEL[action]:
                raise PermissionError("动作超出自主等级")
            grants.append(grant)
        return lineage, grants

    def authorize(self, goal_id, policy, *, level, actor, reason,
                  expected_revision, expires_at, budget_policy=None,
                  no_progress_policy_ref="manual-review/v1"):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        if level not in LEVELS:
            raise ValueError("未知自主等级")
        policy, budget = normalize_policy(policy), _budget({} if budget_policy is None else budget_policy)
        _number(expires_at, "expires_at", positive=True)
        _text(no_progress_policy_ref, "no_progress_policy_ref")
        with self._write():
            goal = self._get(goal_id)
            self._version(goal, expected_revision)
            if goal["status"] in TERMINAL:
                raise PermissionError("终态目标不能重新授权")
            if goal["deadline"] is None or not self.clock() < expires_at <= goal["deadline"]:
                raise ValueError("授权必须受目标期限约束")
            if goal["parent_goal_id"]:
                _, parents = self._grants(goal["parent_goal_id"], "suggest", active=False)
                for parent in parents:
                    if LEVELS.index(level) > LEVELS.index(parent["level"]) or expires_at > parent["expires_at"]:
                        raise PermissionError("子目标扩大自主等级或授权期限")
                    _narrow(policy, parent["policy"])
                    if any(budget[key] > parent["budget_policy"][key] for key in budget):
                        raise PermissionError("子目标扩大任务预算")
            ref = "gauth_" + uuid.uuid4().hex
            grant = dict(authorization_ref=ref, goal_id=goal_id, level=level,
                         policy=policy, expires_at=expires_at, actor=actor,
                         reason=reason, budget_policy=budget,
                         budget_ref="gbudget_" + uuid.uuid4().hex)
            self.db._conn.execute("INSERT INTO goal_grants VALUES (?,?,?,NULL)", (ref, goal_id, _json(grant)))
            goal.update(authorization_ref=ref, budget_ref=grant["budget_ref"],
                        no_progress_policy_ref=no_progress_policy_ref, revision=goal["revision"] + 1)
            return self._save(goal, actor, reason, "goal_authorized")

    def transition(self, goal_id, target, *, actor, reason, expected_revision,
                   evidence_refs=None, command_id=None):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        if target not in GOAL_STATUS_SPEC:
            raise ValueError("非法Goal状态")
        refs = evidence_refs or []
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            raise ValueError("evidence_refs必须为引用数组")
        payload = dict(target=target, actor=actor, reason=reason,
                       expected_revision=expected_revision, evidence_refs=refs)
        with self._write():
            cached, fingerprint = self._command(goal_id, command_id, payload)
            if cached is not None:
                return cached
            goal = self._get(goal_id)
            self._version(goal, expected_revision)
            if target not in TRANSITIONS[goal["status"]]:
                raise GoalConflict("不允许的Goal状态转移")
            if target in {"ready", "running", "review", "completed"}:
                self._grants(goal_id, "suggest", active=False)
                if goal["parent_goal_id"]:
                    self._grants(goal["parent_goal_id"], "suggest")
            if target == "expired" and (goal["deadline"] is None or goal["deadline"] > self.clock()):
                raise GoalConflict("目标尚未过期")
            if target == "completed" and not refs:
                raise ValueError("完成目标必须附验收证据引用")
            goal.update(status=target, revision=goal["revision"] + 1)
            goal["evidence_refs"] = sorted(set(goal["evidence_refs"] + refs))
            result = self._save(goal, actor, reason, "goal_transition")
            return self._remember(goal_id, command_id, fingerprint, result)

    def revoke(self, goal_id, *, actor, reason, expected_revision):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        with self._write():
            goal = self._get(goal_id)
            self._version(goal, expected_revision)
            self.db._conn.execute("UPDATE goal_grants SET revoked_at=? WHERE goal_id=? AND revoked_at IS NULL",
                                  (self.clock(), goal_id))
            if goal["status"] not in TERMINAL:
                goal["status"] = "blocked"
            goal["revision"] += 1
            return self._save(goal, actor, reason, "goal_authorization_revoked")

    def expire_due(self):
        with self._write():
            rows = self.db._conn.execute("SELECT data FROM goals WHERE status NOT IN ('completed','cancelled','expired')").fetchall()
            changed = 0
            for row in rows:
                goal = json.loads(row[0])
                if goal["deadline"] is not None and goal["deadline"] <= self.clock():
                    goal.update(status="expired", revision=goal["revision"] + 1)
                    self._save(goal, "system:deadline", "deadline reached", "goal_expired")
                    changed += 1
                elif goal["authorization_ref"] and goal["status"] != "blocked":
                    grant = self.db._conn.execute("SELECT * FROM goal_grants WHERE authorization_ref=?", (goal["authorization_ref"],)).fetchone()
                    if grant is None or grant["revoked_at"] is not None or json.loads(grant["data"])["expires_at"] <= self.clock():
                        goal.update(status="blocked", revision=goal["revision"] + 1)
                        self._save(goal, "system:authorization", "authorization unavailable", "goal_authorization_expired")
                        changed += 1
            return changed

    def evidence(self, goal_id):
        with self.db._lock:
            self._get(goal_id)
            return [{**dict(row), "data": json.loads(row["data"])} for row in
                    self.db._conn.execute("SELECT * FROM goal_evidence WHERE goal_id=? ORDER BY revision", (goal_id,))]

    def bind_task(self, task, action="execute", *, command_id=None, request_policy=None):
        if action not in {"prepare", "execute", "advance"}:
            raise PermissionError("建议不创建任务；沙箱须走隔离执行入口")
        if request_policy is not None:
            request_policy = normalize_policy(request_policy)
        with self._write():
            scope = "task:" + task.goal_id
            cached, fingerprint = self._command(scope, command_id, dict(
                title=task.user_goal, action=action, budget=task.budget_policy,
                request_policy=request_policy))
            if cached is not None:
                existing = self.db.get_task(cached["task_id"])
                if existing is None:
                    raise GoalConflict("幂等任务记录丢失，需要人工恢复")
                return existing
            lineage, grants = self._grants(task.goal_id, action)
            limits = {key: min(grant["budget_policy"][key] for grant in grants)
                      for key in grants[0]["budget_policy"]}
            requested = task.budget_policy or {}
            if set(requested) - set(limits):
                raise ValueError("Goal任务不允许未知或嵌套预算覆盖")
            for key, value in requested.items():
                _number(value, key)
                limits[key] = min(limits[key], value)
            refs = {goal["goal_id"]: grant["authorization_ref"] for goal, grant in zip(lineage, grants)}
            self.db._conn.execute("INSERT INTO goal_tasks VALUES (?,?,?,?,?,?)",
                                  (task.task_id, task.goal_id, action, _json(refs),
                                   _json(limits), _json(request_policy) if request_policy is not None else None))
            task.budget_policy = limits
            task.runtime_manifest["goal"] = dict(goal_id=task.goal_id,
                authorization_refs=refs, budget_ref=lineage[0]["budget_ref"], action=action)
            # Bind the task and request identity together, before any worker can
            # start. A cross-process replay returns this task without launching it.
            self.db._conn.execute(
                "INSERT INTO tasks (task_id,status,intent,domain,risk_level,user_goal,created_at,data,goal_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (task.task_id, task.task_status.value, task.intent, task.domain,
                 task.risk_level.value, task.user_goal, task.created_at,
                 _json(task.to_dict()), task.goal_id))
            if lineage[0]["status"] == "ready":
                goal = lineage[0]
                goal.update(status="running", revision=goal["revision"] + 1)
                self._save(goal, "runtime:goal", "task bound", "goal_task_created")
            self._remember(scope, command_id, fingerprint, {"task_id": task.task_id})
            return task

    def check_task(self, task_id, contract=None, args=None):
        with self.db._lock:
            row = self.db._conn.execute("SELECT goal_id,data FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if not row["goal_id"]:
                if json.loads(row["data"]).get("origin", "user") != "user":
                    raise PermissionError("自主来源任务缺少Goal授权绑定")
                return None
            binding = self.db._conn.execute("SELECT * FROM goal_tasks WHERE task_id=?", (task_id,)).fetchone()
            if binding is None or binding["goal_id"] != row["goal_id"]:
                raise PermissionError("Goal任务缺少服务端授权绑定")
            try:
                lineage, grants = self._grants(row["goal_id"], binding["action"])
            except KeyError as exc:
                raise PermissionError("Goal引用不存在，需要注册并授权") from exc
            current = {goal["goal_id"]: grant["authorization_ref"] for goal, grant in zip(lineage, grants)}
            if current != json.loads(binding["refs"]):
                raise PermissionError("Goal授权版本已变更，旧任务不得继续")
            if contract is not None:
                if binding["action"] == "prepare" and contract.side_effect_level != "none":
                    raise PermissionError("准备等级禁止实际副作用")
                for grant in grants:
                    check_policy(grant["policy"], contract, args or {})
                if binding["task_policy"] is not None:
                    check_policy(json.loads(binding["task_policy"]), contract, args or {})
            return json.loads(binding["budget_policy"])

    def bind_retry(self, task_id, retry):
        self.check_task(task_id)
        with self.db._lock:
            binding = self.db._conn.execute("SELECT action,task_policy FROM goal_tasks WHERE task_id=?", (task_id,)).fetchone()
            if binding is None:
                raise PermissionError("原Goal任务未获授权")
            policy = json.loads(binding["task_policy"]) if binding["task_policy"] else None
            grant = AuthorizationStore(self.db).current(task_id)
            if grant is not None:
                if grant["revoked_at"] is not None or grant["expires_at"] <= self.clock():
                    raise PermissionError("原任务授权已撤销或过期")
                policy = _intersection(policy, grant["policy"]) if policy is not None else grant["policy"]
        return self.bind_task(retry, binding["action"], request_policy=policy)

    def sandbox(self, goal_id, code, runner, *, actor):
        _text(actor, "actor")
        code = _text(code, "code")
        if len(code) > 65536:
            raise ValueError("沙箱代码过长")
        invocation = "sandbox_" + uuid.uuid4().hex
        with self._write():
            lineage, grants = self._grants(goal_id, "sandbox")
            timeout = min(30, *(grant["budget_policy"]["max_seconds"] for grant in grants))
            if timeout < 0.1:
                raise PermissionError("沙箱预算耗尽")
            goal = lineage[0]
            goal["last_sandbox"] = dict(invocation_id=invocation,
                code_digest=hashlib.sha256(code.encode()).hexdigest(), status="started")
            goal["revision"] += 1
            self._save(goal, actor, "isolated sandbox", "goal_sandbox_started")
        try:
            result = runner.run_code(code, timeout=timeout).to_dict()
        except Exception as exc:
            self._sandbox_result(goal_id, invocation, actor, {"status": "failed", "error_type": type(exc).__name__})
            raise
        self._sandbox_result(goal_id, invocation, actor, result)
        return result

    def _sandbox_result(self, goal_id, invocation, actor, result):
        with self._write():
            goal = self._get(goal_id)
            goal["last_sandbox"] = dict(invocation_id=invocation,
                status=result["status"], result_digest=hashlib.sha256(_json(result).encode()).hexdigest())
            goal["revision"] += 1
            goal["evidence_refs"].append(f"goal:{goal_id}/revision:{goal['revision']}")
            self._save(goal, actor, "sandbox result", "goal_sandbox_finished")
