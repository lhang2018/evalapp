"""Goal administration: roles come from transport auth, never request bodies."""
from ..goals import GoalConflict
from .routes import get, post


def _admin(ctx):
    if ctx.auth.role != "admin":
        ctx.json(403, {"error": "Goal写操作需要admin权限"})
        return False
    return True


def _call(ctx, fn, code=200):
    try:
        return ctx.json(code, fn())
    except GoalConflict as exc:
        return ctx.json(409, {"error": str(exc)})
    except KeyError as exc:
        return ctx.json(404, {"error": str(exc)})
    except PermissionError as exc:
        return ctx.json(403, {"error": str(exc)})
    except (TypeError, ValueError) as exc:
        return ctx.json(400, {"error": str(exc)})


@get("/goals")
def list_goals(ctx):
    _call(ctx, lambda: {"goals": ctx.app.goals.list(
        owner=ctx.query.get("owner"), limit=int(ctx.query.get("limit", 100)))})


@get("/goals/{goal_id}")
def get_goal(ctx):
    _call(ctx, lambda: ctx.app.goals.get(ctx.path.split("/")[2]))


@get("/goals/{goal_id}/evidence")
def get_evidence(ctx):
    _call(ctx, lambda: {"evidence": ctx.app.goals.evidence(ctx.path.split("/")[2])})


@get("/goals/{goal_id}/tasks")
def list_tasks(ctx):
    def load():
        goal_id = ctx.path.split("/")[2]
        ctx.app.goals.get(goal_id)
        tasks = ctx.app.db.list_tasks_by_goal(goal_id)
        return {"tasks": [task.to_dict() for task in tasks if ctx.auth.role == "admin" or
                          ctx.app.memory_bindings.owns(task.task_id, ctx.auth.principal_id)]}
    _call(ctx, load)


@post("/goals")
def create_goal(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goals.create(actor="api:admin", **ctx.body), 201)


@post("/goals/{goal_id}/authorization")
def authorize_goal(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goals.authorize(ctx.path.split("/")[2], actor="api:admin", **ctx.body), 201)


@post("/goals/{goal_id}/transition")
def transition_goal(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goals.transition(ctx.path.split("/")[2], actor="api:admin", **ctx.body))


@post("/goals/{goal_id}/revoke")
def revoke_goal(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goals.revoke(ctx.path.split("/")[2], actor="api:admin", **ctx.body))


@post("/goals/{goal_id}/tasks")
def submit_goal_task(ctx):
    if not _admin(ctx):
        return
    def submit():
        body = dict(ctx.body)
        title = body.pop("goal", None)
        action = body.pop("action", "execute")
        command_id = body.pop("command_id", None)
        if set(body) - {"budget_overrides", "authorization_scope"}:
            raise ValueError("未知Goal任务字段")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("任务goal不能为空")
        task = ctx.app.submit(title, goal_id=ctx.path.split("/")[2],
                              goal_action=action, goal_command_id=command_id,
                              origin="derived", wait=False, principal_id=ctx.auth.principal_id, **body)
        return {"task_id": task.task_id, "goal_id": task.goal_id}
    _call(ctx, submit, 202)


@post("/goals/{goal_id}/sandbox")
def sandbox_goal(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goals.sandbox(ctx.path.split("/")[2],
            ctx.body.get("code"), ctx.app.sandbox, actor="api:admin"))


@post("/goal-policies")
def install_goal_policy(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goal_governor.install_policy(actor="api:admin", **ctx.body), 201)


@post("/goals/{goal_id}/objectives")
def configure_goal_objectives(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goal_governor.configure(ctx.path.split("/")[2], actor="api:admin", **ctx.body))


@get("/goals/{goal_id}/progress")
def get_goal_progress(ctx):
    _call(ctx, lambda: ctx.app.goal_governor.report(ctx.path.split("/")[2]))


@post("/goals/{goal_id}/advance")
def advance_goal(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goal_governor.advance(ctx.path.split("/")[2], **ctx.body))


@post("/goals/{goal_id}/review")
def review_goal_progress(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.goal_governor.review(ctx.path.split("/")[2], actor="api:admin", **ctx.body))


@get("/opportunities")
def list_opportunities(ctx):
    _call(ctx, lambda: {"opportunities": ctx.app.opportunities.ranked(
        policy_ref=ctx.query.get("policy_ref", "manual-review/v1"))})


@post("/opportunities/discover")
def discover_opportunity(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.opportunities.discover(actor="api:admin", **ctx.body), 201)


@post("/opportunities/{candidate_id}/assessment")
def assess_opportunity(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.opportunities.assess(ctx.path.split("/")[2], actor="api:admin", **ctx.body))


@post("/opportunities/{candidate_id}/register")
def register_opportunity(ctx):
    if _admin(ctx):
        _call(ctx, lambda: ctx.app.opportunities.register(ctx.path.split("/")[2], actor="api:admin"), 201)


@get("/opportunities/{candidate_id}/outcome")
def opportunity_outcome(ctx):
    _call(ctx, lambda: ctx.app.opportunities.outcome(ctx.path.split("/")[2]))


# ---- 长期目标周期调度宿主（ALN-13-S）----
@get("/goals/scheduler/status")
def goal_scheduler_status(ctx):
    _call(ctx, lambda: ctx.app.goal_scheduler.status())


@post("/goals/scheduler/tick")
def goal_scheduler_tick(ctx):
    if not _admin(ctx):
        return
    body = ctx.body if isinstance(ctx.body, dict) else {}
    now = body.get("now")
    _call(ctx, lambda: {"report": ctx.app.goal_scheduler.tick(
        now=now)})


@post("/goals/scheduler/start")
def goal_scheduler_start(ctx):
    if not _admin(ctx):
        return
    body = ctx.body if isinstance(ctx.body, dict) else {}
    poll = body.get("poll_seconds")

    def run():
        ctx.app.goal_scheduler.start(
            poll_seconds=float(poll) if poll is not None else None)
        return ctx.app.goal_scheduler.status()
    _call(ctx, run)


@post("/goals/scheduler/stop")
def goal_scheduler_stop(ctx):
    if not _admin(ctx):
        return
    body = ctx.body if isinstance(ctx.body, dict) else {}
    timeout = body.get("timeout", 5.0)

    def run():
        ctx.app.goal_scheduler.stop(timeout=float(timeout))
        return ctx.app.goal_scheduler.status()
    _call(ctx, run)
