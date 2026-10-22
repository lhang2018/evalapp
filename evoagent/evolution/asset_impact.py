"""Join component signals to exact ALN-06 assets and their transitive dependents."""
from ..assets.repository import AssetRepository
from ..assets.content import content_digest


def asset_impact(store, component, capability_roots):
    if store is None or not hasattr(store, "_conn"):
        return {"asset_refs": [], "asset_scope_status": "unavailable", "asset_scope_errors": []}
    repo = AssetRepository(store._conn, ensure_schema=False, lock=store._lock)
    sources = {component}
    sources.update("experts/capabilities/" + root for root in capability_roots)
    parts = component.split("/")
    if len(parts) >= 2 and parts[0] == "expert":
        sources.add("experts/registry/" + parts[1])
    if len(parts) >= 2 and parts[0] == "model":
        sources.add("models/registry/" + parts[1])
    result = {"asset_refs": [], "asset_roots": [], "asset_scope_errors": [],
              "asset_required_suites": [], "asset_required_domains": []}
    try:
        if component.startswith("asset/"):
            roots = [repo.get_exact(component[len("asset/"):])]
        else:
            roots = [a for a in repo.list(limit=2147483647) if sources.intersection(a.source_refs)]
        result["asset_roots"] = sorted(str(a.ref) for a in roots)
        queue, seen, suites, domains = list(roots), set(), set(), set()
        while queue:
            asset = queue.pop()
            if str(asset.ref) in seen:
                continue
            seen.add(str(asset.ref))
            if content_digest(asset.content) != asset.content_digest:
                raise ValueError("资产正文摘要不一致")
            # Exact dependency resolution checks missing versions, pins and cycles.
            repo.check_dependencies(asset)
            result["asset_refs"].append({"asset_id": asset.asset_id, "version": asset.version,
                                         "content_digest": asset.content_digest})
            if asset.content.get("eval_suite"):
                suites.add(asset.content["eval_suite"])
            if asset.content.get("domain") not in (None, "", "shared"):
                domains.add(asset.content["domain"])
            queue.extend(repo.get_exact(str(ref)) for ref in repo.dependents(asset.asset_id, asset.version))
        result["asset_required_suites"] = sorted(suites)
        result["asset_required_domains"] = sorted(domains)
        result["asset_refs"].sort(key=lambda ref: (ref["asset_id"], ref["version"]))
        result["asset_scope_status"] = "resolved" if roots else "not_registered"
    except Exception as exc:
        result["asset_scope_status"] = "invalid"
        result["asset_scope_errors"].append("资产依赖/兼容范围不可核验: " + type(exc).__name__)
    return result
