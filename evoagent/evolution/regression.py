"""E-12 失败案例 → 回归资产转换器（GAP 第二批第 8 项）。

把真实失败转成可复现测试（04 卷 E-12）：已诊断（DIAGNOSED）的诊断案
固化为回归用例（EvalCase），落入运行时回归数据集供评测与门禁复用；
证据不足（OPEN/INCONCLUSIVE、源任务缺失）不转换——保持 unknown，
不把假设写成事实。

转换规则单一来源：core/contracts.py 的 REGRESSION_* 与其文档注释。
ID：一案一案，case_id → reg_<case_id>（幂等，重复执行不产生重复用例）。
"""

from typing import Any, Dict, List

from ..core import contracts as C
from .evaluator import EvalCase, EvalDataset

__all__ = ["FailureRegressionBuilder", "REGRESSION_EVIDENCE_KIND"]

# 回归资产登记簿行类别（复用 db.save_evidence 通用证据行，不新增 schema）：
# 行主键 evidence_id = reg_<case_id>（即幂等键）；转换是确定性重生成
# （源任务+诊断案 → 用例），标记只证明「已转换」，内容可固定重建
REGRESSION_EVIDENCE_KIND = "regression_asset"


class FailureRegressionBuilder:
    """扫描诊断案登记簿，把可证明的失败固化为回归用例。"""

    def __init__(self, controller: Any):
        self.controller = controller

    # ---- 数据集落点 ------------------------------------------------------
    def regression_dataset(self) -> EvalDataset:
        """回归数据集惰性建（复用 controller 自有 datasets 副本）。"""
        ds = self.controller.datasets.get(C.REGRESSION_DATASET_ID)
        if ds is None:
            ds = EvalDataset(
                dataset_id=C.REGRESSION_DATASET_ID, version="1.0.0",
                source="runtime", holdout=False)
            self.controller.datasets[C.REGRESSION_DATASET_ID] = ds
        return ds

    # ---- 主转换入口 ------------------------------------------------------
    def convert_pending(self) -> Dict[str, Any]:
        """扫描诊断案 → 固化回归资产。

        返回 {"converted": [case_id…], "skipped_unknown": [case_id…],
        "already": n}；可重复调用（幂等）。
        """
        store = getattr(self.controller, "store", None)
        out: Dict[str, Any] = {"converted": [], "skipped_unknown": [],
                               "already": 0}
        if store is None:
            return out
        cases = store.list_diagnostic_cases(limit=200)
        # ALN-10 持久化：已转换标记来自证据登记簿（实例重启后仍有效）；
        # 数据集被清空时按标记确定性重建内容
        try:
            markers = {row["evidence_id"]
                       for row in (store.list_evidence(limit=2000)
                                   if hasattr(store, "list_evidence") else [])
                       if row.get("kind") == REGRESSION_EVIDENCE_KIND}
        except Exception:  # noqa: BLE001 —— 登记簿故障不阻断扫描
            markers = set()
        # 惰性落点：没有任何可转换案例时不留空数据集（转换是证据的结果，
        # 不是扫描的副作用）
        dataset = self.controller.datasets.get(C.REGRESSION_DATASET_ID)
        in_dataset = {c.case_id for c in dataset.cases} if dataset else set()
        for case in cases:
            raw_status = getattr(case, "status", "")
            status = str(getattr(raw_status, "value", raw_status) or "").upper()
            cid = getattr(case, "case_id", "")
            if status in C.REGRESSION_UNKNOWN_STATUSES:
                out["skipped_unknown"].append(cid)
                continue
            if status not in C.REGRESSION_SOURCE_STATUSES:
                continue
            reg_id = f"reg_{cid}"
            if reg_id in in_dataset:
                out["already"] += 1
                continue
            if reg_id in markers:
                # 标记在库：确定重建内容（重启/数据集重置恢复路径）
                asset = self._to_case(case)
                if asset is None:
                    out["skipped_unknown"].append(cid)
                    continue
                if dataset is None:
                    dataset = self.regression_dataset()
                dataset.cases.append(asset)
                out["already"] += 1
                continue
            asset = self._to_case(case)
            if asset is None:
                # 源任务缺失 = 无法复现目标（证据不足，不转换）
                out["skipped_unknown"].append(cid)
                continue
            if dataset is None:
                dataset = self.regression_dataset()
            dataset.cases.append(asset)
            self._record_marker(store, asset,
                                f"reg_{cid}", getattr(case, "case_id", ""),
                                getattr(case, "task_id", ""))
            out["converted"].append(reg_id)
        return out

    @staticmethod
    def _record_marker(store: Any, asset: EvalCase, reg_id: str,
                       case_id: str, task_id: str) -> None:
        """登记「已转换」证据行（登记簿故障不阻断转换本身）。"""
        try:
            store.save_evidence(
                evidence_id=reg_id, task_id=task_id or None,
                kind=REGRESSION_EVIDENCE_KIND,
                ref=f"{C.REGRESSION_DATASET_ID}/{reg_id}",
                summary=(f"失败回归资产（源自诊断案 {case_id}）："
                         f"{asset.input_goal[:80]}"),
            )
        except Exception:  # noqa: BLE001
            pass

    # ---- 单案转换规则 ----------------------------------------------------
    def _to_case(self, case: Any) -> EvalCase:
        store = self.controller.store
        task = getattr(case, "task_id", None)
        task = store.get_task(task) if task and store is not None else None
        if task is None:
            # 源任务缺失 = 无法复现目标（证据不足，不转换）
            return None
        acceptance = list(getattr(task, "acceptance_criteria", None) or [])
        if not acceptance:
            acceptance = [{"type": "result_nonempty"}]
        tags = [
            C.REGRESSION_TAG, "auto",
            f"diagnostic_case/{getattr(case, 'case_id', '')}",
            f"task/{getattr(case, 'task_id', '')}",
        ]
        return EvalCase(
            case_id=f"reg_{getattr(case, 'case_id', '')}",
            input_goal=getattr(task, "user_goal", "") or "",
            intent=getattr(task, "intent", "") or "general",
            domain=getattr(task, "domain", "") or "general",
            expected=acceptance,
            difficulty="medium",
            holdout=False,
            safety=False,
            tags=tags,
        )
