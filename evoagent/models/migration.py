"""模型迁移执行链（PRD 20 / 104 / 111, Architecture 7.4）。

迁移不是改一个配置项：必须先识别继承资产与受影响组件，做兼容性检查、
同数据集回归、质量/成本/时延对比，生成新的发布组合，最后受控启用并保留
旧模型与回滚路径。任何一步失败都不能直接切换。
"""

import dataclasses
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.events import EventBus
from ..evolution.evaluator import EvalDataset, EvalReport, Evaluator, TaskRunner
from .base import ModelProfile, ModelRegistry
from .router import ModelRouter

MIGRATION_STEPS = [
    "profile", "assets", "components", "compatibility",
    "regression", "comparison", "release", "enable", "rollback",
]

STEP_LABELS = {
    "profile": "创建/校验新模型档案",
    "assets": "识别可继承的能力资产",
    "components": "识别受影响组件",
    "compatibility": "API 与工具兼容性测试",
    "regression": "领域回归",
    "comparison": "质量/成本/时延对比",
    "release": "生成新发布组合",
    "enable": "受控验证后启用",
    "rollback": "保留旧模型与回滚路径",
}


@dataclass
class MigrationPolicy:
    max_quality_drop: float = 0.05
    max_cost_increase_ratio: float = 1.5
    max_latency_increase_ratio: float = 1.5
    min_success_rate: float = 0.6
    max_tool_failure_rate: float = 0.2


@dataclass
class ModelMigration:
    migration_id: str
    old_model_id: str
    new_model_id: str
    status: str = "planned"          # planned|compatible|evaluated|ready|applied|blocked|rolled_back
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    dataset_id: str = ""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    compatibility: Dict[str, Any] = field(default_factory=dict)
    inherited_assets: Dict[str, Any] = field(default_factory=dict)
    affected_routes: Dict[str, str] = field(default_factory=dict)
    regression: Dict[str, Any] = field(default_factory=dict)
    release_id: Optional[str] = None
    blocked_reasons: List[str] = field(default_factory=list)
    applied_state: Dict[str, Any] = field(default_factory=dict)
    applied_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelMigration":
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


class ModelMigrationManager:
    """执行 PRD 20 的九步迁移流程；状态持久化，可中断续跑。"""

    def __init__(self, registry: ModelRegistry, router: ModelRouter, *,
                 runner_factory: Optional[Callable[[str], TaskRunner]] = None,
                 datasets: Optional[Dict[str, EvalDataset]] = None,
                 releases: Optional[Any] = None,
                 db: Optional[Any] = None,
                 bus: Optional[EventBus] = None,
                 policy: Optional[MigrationPolicy] = None,
                 default_dataset_id: Optional[str] = None):
        self.registry = registry
        self.router = router
        self.runner_factory = runner_factory
        self.datasets = datasets or {}
        self.releases = releases
        self.db = db
        self.bus = bus
        self.policy = policy or MigrationPolicy()
        self.default_dataset_id = default_dataset_id
        self._records: Dict[str, ModelMigration] = {}

    # ================= 创建 =================
    def create(self, old_model_id: str, new_model_id: str, *,
               dataset_id: Optional[str] = None) -> ModelMigration:
        if not old_model_id or not new_model_id:
            raise ValueError("必须提供 old_model_id 与 new_model_id")
        if old_model_id == new_model_id:
            raise ValueError("新旧模型不能相同")
        old_profile = self.registry.get(old_model_id)
        if old_profile is None:
            raise KeyError(f"旧模型档案不存在: {old_model_id}")
        new_profile = self.registry.get(new_model_id)
        if new_profile is None:
            raise KeyError(f"新模型档案不存在: {new_model_id}")

        mig = ModelMigration(
            migration_id=uuid.uuid4().hex[:12],
            old_model_id=old_model_id,
            new_model_id=new_model_id,
            dataset_id=dataset_id or self._default_dataset(),
            steps=[{"step": s, "label": STEP_LABELS[s], "status": "pending",
                    "detail": {}}
                   for s in MIGRATION_STEPS])
        self._set_step(mig, "profile", "done",
                       {"provider": new_profile.provider,
                        "context_limit": new_profile.context_limit})
        mig.inherited_assets = self._inherited_assets(old_profile)
        self._set_step(mig, "assets", "done", {
            "manifest": mig.inherited_assets.get("source_release_id"),
            "prompt_version": mig.inherited_assets.get("prompt_version"),
            "tools_version": mig.inherited_assets.get("tools_version"),
            "workflow_versions": mig.inherited_assets.get("workflow_versions"),
        })
        mig.affected_routes = {
            key: model for key, model in self.router.routes().items()
            if model == old_model_id}
        self._set_step(mig, "components", "done", {
            "routes": mig.affected_routes,
            "count": len(mig.affected_routes)})
        self._check_compatibility(mig)
        self._save(mig)
        self._emit("model_migration_created", {
            "migration_id": mig.migration_id,
            "old_model_id": old_model_id, "new_model_id": new_model_id,
            "status": mig.status})
        return mig

    # ================= 兼容性 =================
    def _check_compatibility(self, mig: ModelMigration) -> None:
        old = self.registry.get(mig.old_model_id)
        new = self.registry.get(mig.new_model_id)
        checks: List[Dict[str, Any]] = []
        failures: List[str] = []
        warnings: List[str] = []

        def check(name: str, ok: bool, detail: str, *, blocking: bool = True):
            checks.append({"name": name, "passed": bool(ok),
                           "detail": detail, "blocking": blocking})
            if not ok:
                (failures if blocking else warnings).append(detail)

        check("profile", new is not None, "新模型档案存在")
        check("available", bool(new and new.available), "新模型可用")
        adapter_ok = self.router.resolve(mig.new_model_id) is not None
        check("adapter", adapter_ok,
              "适配器可按 model_id 解析" if adapter_ok
              else "适配器无法按 model_id 解析（未知模型不得静默回退）")
        if old is not None and new is not None:
            if old.supports_tool_call:
                check("tool_call", new.supports_tool_call,
                      "新模型支持工具调用")
            if old.supports_structured_output:
                check("structured_output", new.supports_structured_output,
                      "新模型支持结构化输出")
            if old.supports_vision:
                check("vision", new.supports_vision,
                      "新模型支持视觉输入", blocking=False)
            if new.context_limit < old.context_limit:
                warnings.append(
                    f"上下文窗口缩小: {old.context_limit} → {new.context_limit}")
            if new.rpm_limit < old.rpm_limit:
                warnings.append(
                    f"RPM 限制下降: {old.rpm_limit} → {new.rpm_limit}")
            for limitation in new.known_limitations:
                warnings.append(f"已知限制: {limitation}")
        passed = not failures
        if new is not None:
            self.registry.record_verification(mig.new_model_id, passed)
        mig.compatibility = {
            "passed": passed, "checks": checks,
            "failures": failures, "warnings": warnings,
            "affected_routes": mig.affected_routes,
            "old_model": dataclasses.asdict(old) if old else None,
            "new_model": dataclasses.asdict(new) if new else None,
        }
        if passed:
            mig.status = "compatible"
            self._set_step(mig, "compatibility", "done", {
                "checks": len(checks), "warnings": warnings})
        else:
            mig.status = "blocked"
            mig.blocked_reasons = list(failures)
            self._set_step(mig, "compatibility", "failed",
                           {"failures": failures})
            self._emit("model_migration_blocked", {
                "migration_id": mig.migration_id,
                "reasons": failures}, severity="warn")

    # ================= 回归与对比 =================
    def run_regression(self, migration_id: str, *,
                       dataset_id: Optional[str] = None) -> ModelMigration:
        mig = self._require(migration_id)
        if mig.status in ("applied", "rolled_back"):
            raise RuntimeError(f"迁移状态 {mig.status} 不能运行回归")
        if not mig.compatibility.get("passed"):
            raise RuntimeError("兼容性检查未通过，不能运行回归")
        mig.blocked_reasons = []
        if self.runner_factory is None:
            raise RuntimeError("未注入评测执行器")
        dataset = self.datasets.get(dataset_id or mig.dataset_id)
        if dataset is None:
            raise KeyError(f"评测集不存在: {dataset_id or mig.dataset_id}")
        mig.dataset_id = dataset.dataset_id
        baseline = self._evaluate(dataset, mig.old_model_id, baseline=True)
        candidate = self._evaluate(dataset, mig.new_model_id, baseline=False)
        comparison = self._compare(baseline, candidate)
        mig.regression = {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "baseline": baseline.summary(),
            "candidate": candidate.summary(),
            "comparison": comparison,
        }
        self._set_step(mig, "regression",
                       "done" if comparison["cases_ok"] else "failed",
                       {"case_count": candidate.case_count,
                        "candidate_pass": candidate.pass_count})
        self._set_step(mig, "comparison",
                       "done" if comparison["passed"] else "failed",
                       comparison)
        if comparison["passed"]:
            mig.status = "evaluated"
            self._emit("model_migration_evaluated", {
                "migration_id": mig.migration_id,
                "quality_delta": comparison["quality_delta"],
                "cost_ratio": comparison["cost_ratio"],
                "latency_ratio": comparison["latency_ratio"]})
        else:
            mig.status = "blocked"
            mig.blocked_reasons = list(comparison["reasons"])
            self._emit("model_migration_blocked", {
                "migration_id": mig.migration_id,
                "reasons": comparison["reasons"]}, severity="warn")
        self._save(mig)
        return mig

    def _evaluate(self, dataset: EvalDataset, model_id: str,
                  *, baseline: bool) -> EvalReport:
        runner = self.runner_factory(model_id)
        evaluator = Evaluator(runner, self.bus)
        return evaluator.run(dataset, baseline=baseline, metadata={
            "model_id": model_id,
            "migration": True,
            "dataset_version": dataset.version,
        })

    def _compare(self, baseline: EvalReport,
                 candidate: EvalReport) -> Dict[str, Any]:
        p = self.policy
        reasons: List[str] = []
        cases_ok = candidate.case_count > 0 and (
            candidate.dataset_id, candidate.version) == (
            baseline.dataset_id, baseline.version)
        if not cases_ok:
            reasons.append("回归数据集不一致或为空")
        if candidate.case_count and candidate.success_rate < p.min_success_rate:
            reasons.append(
                f"新模型成功率不足: {round(candidate.success_rate, 3)}"
                f" < {p.min_success_rate}")
        quality_delta = round(candidate.success_rate - baseline.success_rate, 4)
        if quality_delta < -p.max_quality_drop:
            reasons.append(f"质量回退: {quality_delta}")
        tool_rate = candidate.tool_failures / max(1, candidate.case_count)
        if tool_rate > p.max_tool_failure_rate:
            reasons.append(f"工具失败率过高: {round(tool_rate, 3)}")
        cost_ratio = _ratio(candidate.cost, baseline.cost)
        if cost_ratio is not None and cost_ratio > p.max_cost_increase_ratio:
            reasons.append(f"成本增幅过大: x{round(cost_ratio, 2)}")
        latency_ratio = _ratio(candidate.avg_latency, baseline.avg_latency)
        if latency_ratio is not None \
                and latency_ratio > p.max_latency_increase_ratio:
            reasons.append(f"时延增幅过大: x{round(latency_ratio, 2)}")
        return {
            "passed": not reasons,
            "cases_ok": cases_ok,
            "reasons": reasons,
            "quality_delta": quality_delta,
            "cost_delta": round(candidate.cost - baseline.cost, 6),
            "cost_ratio": round(cost_ratio, 4) if cost_ratio is not None else None,
            "latency_delta": round(
                candidate.avg_latency - baseline.avg_latency, 4),
            "latency_ratio": round(latency_ratio, 4)
            if latency_ratio is not None else None,
            "tool_failure_rate": round(tool_rate, 4),
        }

    # ================= 发布与启用 =================
    def prepare_release(self, migration_id: str) -> ModelMigration:
        mig = self._require(migration_id)
        if mig.status != "evaluated" or not mig.regression.get("comparison", {}) \
                .get("passed"):
            raise RuntimeError("迁移尚未通过回归对比，不能生成发布组合")
        if self.releases is None:
            raise RuntimeError("发布管理器不可用")
        assets = mig.inherited_assets
        comparison = mig.regression["comparison"]
        manifest = self.releases.build_manifest(
            version=f"migrate-{mig.new_model_id}-{mig.migration_id[:6]}",
            code_commit=assets.get("code_commit", "local"),
            model_adapter_version=mig.new_model_id,
            prompt_version=assets.get("prompt_version", "v0"),
            tools_version=assets.get("tools_version", "builtin-1.0.0"),
            workflow_versions=list(assets.get("workflow_versions") or []),
            memory_policy_version=assets.get("memory_policy_version", "v0"),
            env_version=assets.get("env_version", "local"),
            eval_dataset_version=(
                f"{mig.regression.get('dataset_id')}"
                f"@{mig.regression.get('dataset_version')}"),
            eval_report={
                "migration_id": mig.migration_id,
                "quality_delta": comparison["quality_delta"],
                "cost_ratio": comparison["cost_ratio"],
                "latency_ratio": comparison["latency_ratio"],
            },
            compatible_requirements=[f"model:{mig.old_model_id}",
                                     f"model:{mig.new_model_id}"],
            rollback_target=self.releases.current_release_id,
        )
        mig.release_id = manifest.release_id
        mig.status = "ready"
        self._set_step(mig, "release", "done", {
            "release_id": manifest.release_id,
            "rollback_target": manifest.rollback_target})
        self._set_step(mig, "rollback", "done", {
            "target": manifest.rollback_target,
            "old_model_id": mig.old_model_id})
        self._save(mig)
        self._emit("model_migration_release_ready", {
            "migration_id": mig.migration_id,
            "release_id": manifest.release_id,
            "rollback_target": manifest.rollback_target})
        return mig

    def apply(self, migration_id: str, *, actor: str = "operator",
              gray_percent: int = 100,
              activate_default: Optional[bool] = None) -> Dict[str, Any]:
        mig = self._require(migration_id)
        if mig.status != "ready":
            raise RuntimeError(f"迁移状态 {mig.status} 不能启用"
                               f"（需先通过回归并生成发布组合）")
        if mig.release_id and self.releases is not None:
            deployed = self.releases.deploy(
                mig.release_id, gate_passed=True, gray_percent=gray_percent)
            if not deployed.get("ok"):
                raise RuntimeError(
                    f"发布启用失败: {deployed.get('error')}")
        mig.applied_state = {
            "routes": dict(mig.affected_routes),
            "default_model_id": self.router.default_model_id,
        }
        for key in mig.affected_routes:
            self.router.bind_route(key, mig.new_model_id)
        if activate_default is None:
            activate_default = not mig.affected_routes
        if activate_default:
            self.router.set_default_model(mig.new_model_id)
        mig.status = "applied"
        mig.applied_at = time.time()
        mig.updated_at = mig.applied_at
        self._set_step(mig, "enable", "done", {
            "routes": mig.affected_routes,
            "default_model_id": self.router.default_model_id,
            "actor": actor, "gray_percent": gray_percent})
        self._save(mig)
        self._emit("model_migration_applied", {
            "migration_id": mig.migration_id,
            "old_model_id": mig.old_model_id,
            "new_model_id": mig.new_model_id,
            "routes": mig.affected_routes,
            "default_model_id": self.router.default_model_id,
            "actor": actor})
        return {"ok": True, "migration": mig.to_dict(),
                "routes": mig.affected_routes,
                "default_model_id": self.router.default_model_id}

    def rollback(self, migration_id: str, reason: str = "") -> Dict[str, Any]:
        mig = self._require(migration_id)
        if mig.status != "applied":
            raise RuntimeError(f"迁移状态 {mig.status} 不能回滚")
        state = mig.applied_state or {}
        for key, model_id in (state.get("routes") or {}).items():
            self.router.bind_route(key, model_id)
        self.router.set_default_model(state.get("default_model_id"))
        release = None
        if self.releases is not None:
            release = self.releases.rollback(reason or "model migration rollback")
        mig.status = "rolled_back"
        mig.updated_at = time.time()
        self._set_step(mig, "enable", "rolled_back", {"reason": reason})
        self._save(mig)
        self._emit("model_migration_rolled_back", {
            "migration_id": mig.migration_id,
            "reason": reason, "routes": state.get("routes"),
            "old_model_id": mig.old_model_id}, severity="warn")
        return {"ok": True, "migration": mig.to_dict(), "release": release}

    # ================= 查询 =================
    def get(self, migration_id: str) -> Optional[ModelMigration]:
        raw = self._records.get(migration_id)
        if raw is not None:
            return raw
        if self.db is not None:
            data = self.db.get_model_migration(migration_id)
            if data:
                mig = ModelMigration.from_dict(data)
                self._records[migration_id] = mig
                return mig
        return None

    def list(self, limit: int = 50) -> List[ModelMigration]:
        if self.db is not None:
            return [ModelMigration.from_dict(d)
                    for d in self.db.list_model_migrations(limit=limit)]
        return sorted(self._records.values(),
                      key=lambda m: m.created_at, reverse=True)[:limit]

    # ================= 内部 =================
    def _require(self, migration_id: str) -> ModelMigration:
        mig = self.get(migration_id)
        if mig is None:
            raise KeyError(f"迁移记录不存在: {migration_id}")
        return mig

    def _default_dataset(self) -> str:
        if self.default_dataset_id:
            return self.default_dataset_id
        if "builtin_smoke" in self.datasets:
            return "builtin_smoke"
        for ds in self.datasets.values():
            if not ds.is_holdout():
                return ds.dataset_id
        return ""

    def _inherited_assets(self, old_profile: ModelProfile) -> Dict[str, Any]:
        assets: Dict[str, Any] = {
            "code_commit": "local",
            "prompt_version": "v0",
            "tools_version": "builtin-1.0.0",
            "workflow_versions": [],
            "memory_policy_version": "v0",
            "env_version": "local",
            "source_release_id": None,
            "old_model_profile": dataclasses.asdict(old_profile),
        }
        if self.releases is not None and self.releases.current_release_id:
            m = self.releases.manifests.get(self.releases.current_release_id)
            if m is not None:
                data = m.to_dict()
                for key in ("code_commit", "prompt_version", "tools_version",
                            "workflow_versions", "memory_policy_version",
                            "env_version"):
                    assets[key] = data.get(key, assets[key])
                assets["source_release_id"] = m.release_id
        return assets

    @staticmethod
    def _set_step(mig: ModelMigration, key: str, status: str,
                  detail: Optional[Dict[str, Any]] = None) -> None:
        for step in mig.steps:
            if step.get("step") == key:
                step["status"] = status
                step["detail"] = detail or {}
        mig.updated_at = time.time()

    def _save(self, mig: ModelMigration) -> None:
        self._records[mig.migration_id] = mig
        if self.db is not None:
            self.db.save_model_migration(
                mig.migration_id, mig.old_model_id, mig.new_model_id,
                mig.status, mig.to_dict())

    def _emit(self, event_type: str, payload: Dict[str, Any],
              severity: str = "info") -> None:
        if self.bus is not None:
            self.bus.emit(event_type=event_type, component_id="model_migration",
                          severity=severity, payload=payload)


def _ratio(candidate: float, baseline: float) -> Optional[float]:
    """成本/时延比值；基线为 0 时无法比较（返回 None，不制造虚假通过）。"""
    if baseline and baseline > 0:
        return candidate / baseline
    return None
