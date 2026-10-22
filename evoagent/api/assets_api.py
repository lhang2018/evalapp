"""能力资产接口（契约 §6 / F-11 / ALN-06-A）。注册表路由。

只暴露资产域能力：精确版本登记/读取、依赖解析、生命周期迁移、引用查询。
`server.py` 的注册行属于共享接线，按任务卡单列补丁合入。
"""

from .routes import get, post

_MAX_LIST = 500


def _repo(ctx):
    from ..assets.repository import repository_for

    return repository_for(ctx.app.db)


def _error(ctx, exc) -> None:
    from ..assets.entity import (AssetConflict, AssetNotFound,
                                 AssetValidationError, DependencyError,
                                 LifecycleError, PackVerificationError)

    if isinstance(exc, AssetNotFound):
        code = 404
    elif isinstance(exc, AssetConflict):
        code = 409
    elif isinstance(exc, (DependencyError, LifecycleError)):
        code = 409
    elif isinstance(exc, (AssetValidationError, PackVerificationError,
                          ValueError, TypeError)):
        code = 400
    else:
        code = 500
    ctx.json(code, {"error": str(exc), "error_class": type(exc).__name__})


@get("/assets")
def list_assets(ctx):
    """按类型/命名空间/生命周期筛选资产清单（契约字段，不含内容）。"""
    try:
        limit = min(max(int(ctx.query.get("limit", "100")), 1), _MAX_LIST)
    except (TypeError, ValueError):
        ctx.json(400, {"error": "非法 limit"})
        return
    try:
        repo = _repo(ctx)
        assets = repo.list(
            asset_type=ctx.query.get("asset_type") or None,
            namespace=ctx.query.get("namespace") or None,
            lifecycle_state=ctx.query.get("lifecycle_state") or None,
            limit=limit)
        ctx.json(200, {"assets": [a.summary() for a in assets],
                       "count": len(assets)})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@get("/assets/mapping")
def asset_inventory_mapping(ctx):
    """现有专家/工作流/模型清单到 Asset 的只读映射（含缺失信息）。"""
    try:
        from ..assets.mapping import map_inventory

        app = ctx.app
        experts = app.experts.all() if getattr(app, "experts", None) else []
        workflows = list(getattr(app, "_workflows", {}).values())
        models = (app.model_registry.all()
                  if getattr(app, "model_registry", None) else [])
        drafts = map_inventory(experts=experts, workflows=workflows,
                               models=models)
        ctx.json(200, {
            "mappings": [{
                "asset_id": d.asset_id,
                "asset_type": d.asset_type,
                "namespace": d.namespace,
                "version": d.version,
                "lifecycle_state": d.lifecycle_state,
                "source_refs": list(d.source_refs),
                "dependency_refs": list(d.dependency_refs),
                "missing": list(d.missing),
                "notes": list(d.notes),
            } for d in drafts],
            "count": len(drafts),
            "note": "映射不自动晋升；能力目录条目需由持有方提供版本后再登记",
        })
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@post("/assets")
def register_asset(ctx):
    """登记一个不可变版本。同 ID/version 内容不同 → 409，不覆盖。"""
    try:
        from ..assets.repository import _as_sequence

        body = ctx.body or {}
        content = body.get("content")
        if not isinstance(content, dict):
            ctx.json(400, {"error": "content 必须是 JSON 对象"})
            return
        repo = _repo(ctx)
        asset = repo.register(
            asset_id=str(body.get("asset_id") or ""),
            asset_type=str(body.get("asset_type") or ""),
            namespace=str(body.get("namespace") or ""),
            version=str(body.get("version") or ""),
            content=content,
            lifecycle_state=str(body.get("lifecycle_state") or "draft"),
            source_refs=_as_sequence(body.get("source_refs")),
            dependency_refs=_as_sequence(body.get("dependency_refs")),
            compatibility=dict(body.get("compatibility") or {}),
            evaluation_evidence_refs=_as_sequence(
                body.get("evaluation_evidence_refs")),
            rollback_ref=body.get("rollback_ref") or None,
            content_digest_expected=body.get("content_digest") or None)
        ctx.json(201, {"asset": asset.summary()})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@get("/assets/gc/candidates")
def asset_gc_candidates(ctx):
    """可回收候选：仅 retired 且无精确引用。"""
    try:
        ctx.json(200, {"candidates": _repo(ctx).gc_candidates()})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@get("/assets/{asset_id}/{version}")
def get_asset_exact(ctx):
    """按精确版本读取；不存在即 404，绝不回退到其他版本。"""
    try:
        asset = _repo(ctx).get_exact(
            f"{ctx.path.split('/')[2]}@{ctx.path.split('/')[3]}")
        ctx.json(200, {"asset": asset.summary(), "content": asset.content})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@post("/assets/{asset_id}/{version}/lifecycle")
def transition_asset(ctx):
    """生命周期迁移；晋升 approved/active 需证据、来源与可解析依赖。"""
    try:
        from ..assets.repository import _as_sequence

        parts = ctx.path.split("/")
        body = ctx.body or {}
        repo = _repo(ctx)
        asset = repo.transition(
            parts[2], parts[3], str(body.get("to_state") or ""),
            evidence_refs=_as_sequence(body.get("evidence_refs")))
        ctx.json(200, {"asset": asset.summary()})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@get("/assets/{asset_id}/{version}/ref")
def asset_manifest_ref(ctx):
    """冻结清单引用：asset_id + version + content_digest，不含内容。"""
    try:
        from ..assets.refs import manifest_asset_ref

        parts = ctx.path.split("/")
        asset = _repo(ctx).get_exact(f"{parts[2]}@{parts[3]}")
        ctx.json(200, {"ref": manifest_asset_ref(asset)})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@get("/assets/{asset_id}/{version}/dependencies")
def asset_dependencies(ctx):
    """解析依赖：缺失/环/不兼容 → 409，不返回半份依赖表。"""
    try:
        parts = ctx.path.split("/")
        repo = _repo(ctx)
        asset = repo.get_exact(f"{parts[2]}@{parts[3]}")
        deps = repo.check_dependencies(asset)
        ctx.json(200, {"asset": str(asset.ref),
                       "dependencies": [
                           {"ref": ref, "lifecycle_state": dep.lifecycle_state,
                            "content_digest": dep.content_digest}
                           for ref, dep in sorted(deps.items())]})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@get("/assets/{asset_id}/{version}/dependents")
def asset_dependents(ctx):
    """引用查询：谁精确引用了这个版本（别名不算引用）。"""
    try:
        parts = ctx.path.split("/")
        refs = _repo(ctx).dependents(parts[2], parts[3])
        ctx.json(200, {"asset": f"{parts[2]}@{parts[3]}",
                       "dependents": [str(r) for r in refs],
                       "count": len(refs)})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)


@get("/assets/{asset_id}/{version}/events")
def asset_events(ctx):
    """登记/迁移/回收的可追溯台账。"""
    try:
        parts = ctx.path.split("/")
        events = _repo(ctx).events(parts[2], parts[3])
        ctx.json(200, {"asset": f"{parts[2]}@{parts[3]}",
                       "events": events})
    except Exception as exc:  # noqa: BLE001
        _error(ctx, exc)
