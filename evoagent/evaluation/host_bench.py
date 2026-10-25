"""真实宿主上的候选对照基准与收益/依赖回归证据（ALN-10-E）。

消费 ALN-09-H 的隔离候选宿主（真实资产库版本 + 摘要校验 + 一次性沙箱）
与 ALN-10-H 的独立门禁结论，回答三个问题并留下可复核证据：

1. **对照可复现**：同一 ``asset_id@version``（含 ``content_digest``）在同一基准
   版本上重复执行结果一致；不一致即判 ``reproducible=False``，收益结论作废
   （fail-closed），不允许拿噪声当收益。
2. **实际覆盖有范围/遗漏说明**：每个用例标记是否必跑与所属领域；超时、越界
   违规、崩溃、输出不可解析都进 ``coverage.omitted`` 并给原因，覆盖率不足
   直接阻止发布，而不是静默跳过。
3. **退化阻止发布或触发既定回滚**：候选在任一必跑用例上由通过变失败即
   ``regression_cases`` 非空 → ``release_decision.allow=False``；发布后监测
   证据（``benefit_chain`` 的 ``post_release``）显示退化 → ``rollback_required=True``。

候选资产执行协议（与 09-H 宿主一致）：
- 资产 ``content = {"entry": "main.py", "files": {"main.py": "..."}}``；
- 基准把用例输入写入沙箱内 ``input.json``，入口脚本读取该文件；
- 入口在 stdout 最后一行输出一个 JSON 对象：``{"case_id": "...", "ok": true,
  "value": ...}``；其它输出被忽略，不解析到 JSON 视为 ``invalid_output``。

边界：本轮基准是本地受控基准（非生产流量），对照结论只对本卡绑定的
资产摘要与基准版本成立；不宣称线上收益。
"""

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..assets.content import content_digest
from ..evolution.host import IsolatedCandidateHost

BENCH_VERSION = "host-bench/1"
INPUT_FILE = "input.json"


class BenchError(ValueError):
    """基准配置或输入非法。"""


@dataclass(frozen=True)
class BenchCase:
    case_id: str
    input: Dict[str, Any] = field(default_factory=dict)
    domain: str = "general"
    required: bool = True


@dataclass(frozen=True)
class BenchObservation:
    case_id: str
    status: str               # ok | failed | timeout | violation | crashed | invalid_output
    ok: bool
    value: Any = None
    duration_s: float = 0.0
    violations: tuple = ()
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"case_id": self.case_id, "status": self.status,
                "ok": bool(self.ok), "value": self.value,
                "duration_s": round(float(self.duration_s), 4),
                "violations": list(self.violations), "error": self.error}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def evidence_digest(report: Dict[str, Any]) -> str:
    """证据摘要：绑定资产版本/基准版本/逐用例结论与最终裁决。

    刻意不绑定耗时等机器噪声字段，否则同一资产重复对照会得到不同摘要，
    「对照可复现」就无从核对；耗时仍在报告里单独记录。
    """
    coverage = report.get("coverage") or {}
    benefit = report.get("benefit") or {}
    body = {
        "benchmark_version": report.get("benchmark_version"),
        "baseline": report.get("baseline"),
        "candidate": report.get("candidate"),
        "repeat": report.get("repeat"),
        "source": report.get("source"),
        "cases_digest": report.get("cases_digest"),
        "reproducible": report.get("reproducible"),
        "regression_cases": report.get("regression_cases"),
        "verdict": report.get("verdict"),
        "coverage": {"total": coverage.get("total"),
                     "covered": coverage.get("covered"),
                     "required_coverage_ok": coverage.get("required_coverage_ok"),
                     "omitted": coverage.get("omitted")},
        "benefit": {"verdict": benefit.get("verdict"),
                    "success_delta": benefit.get("success_delta"),
                    "blockers": benefit.get("blockers")},
    }
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def _last_json(stdout: str) -> Optional[Dict[str, Any]]:
    for line in reversed([item for item in (stdout or "").splitlines() if item.strip()]):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class HostBenchmark:
    """在隔离宿主上对照执行同一组用例，产出收益与依赖回归证据。"""

    def __init__(self, host: IsolatedCandidateHost, *, timeout: float = 5.0,
                 repeat: int = 2, min_coverage: float = 0.8,
                 latency_tolerance: float = 0.25, max_violations: int = 0,
                 source: str = ""):
        if host is None:
            raise BenchError("HostBenchmark 需要 IsolatedCandidateHost")
        if type(repeat) is not int or not 1 <= repeat <= 5:
            raise BenchError("repeat 必须是 1..5 的整数")
        if not 0 < min_coverage <= 1:
            raise BenchError("min_coverage 必须在 (0, 1]")
        if not 0 <= latency_tolerance < 10:
            raise BenchError("latency_tolerance 非法")
        self.host = host
        self.sandbox = getattr(host, "sandbox", None)
        if self.sandbox is None:
            raise BenchError("宿主缺少 sandbox，无法执行候选资产")
        self.timeout = float(timeout)
        self.repeat = repeat
        self.min_coverage = float(min_coverage)
        self.latency_tolerance = float(latency_tolerance)
        self.max_violations = int(max_violations)
        self.source = source

    # ---- 单资产执行 ----
    def observe(self, ref: Any, case: BenchCase) -> List[BenchObservation]:
        """执行一个精确资产版本在单个用例上的 ``repeat`` 次结果。"""
        artifact = self.host.resolve(ref)  # 校验摘要，拒绝非代码/被篡改资产
        files = dict(artifact.files)
        files[INPUT_FILE] = _canonical(dict(case.input))
        observations = []
        for _ in range(self.repeat):
            result = self.sandbox.run_project(files, entry=artifact.entry,
                                              timeout=self.timeout)
            observations.append(self._classify(case, result))
        return observations

    @staticmethod
    def _classify(case: BenchCase, result: Any) -> BenchObservation:
        violations = tuple(result.violations)
        if violations:
            return BenchObservation(case.case_id, "violation", False,
                                    duration_s=result.duration_s,
                                    violations=violations,
                                    error="; ".join(violations))
        if result.status == "timeout":
            return BenchObservation(case.case_id, "timeout", False,
                                    duration_s=result.duration_s)
        if result.status == "failed":
            detail = (result.stderr or "").strip().splitlines()
            return BenchObservation(case.case_id, "crashed", False,
                                    duration_s=result.duration_s,
                                    error=detail[-1] if detail else "")
        payload = _last_json(result.stdout)
        if payload is None:
            return BenchObservation(case.case_id, "invalid_output", False,
                                    duration_s=result.duration_s,
                                    error="stdout 最后一行不是 JSON 对象")
        if payload.get("case_id") not in (None, case.case_id):
            return BenchObservation(case.case_id, "invalid_output", False,
                                    duration_s=result.duration_s,
                                    error="回显 case_id 与用例不一致")
        return BenchObservation(case.case_id,
                                "ok" if payload.get("ok") else "failed",
                                bool(payload.get("ok")),
                                value=payload.get("value"),
                                duration_s=result.duration_s)

    def _deterministic(self, rows: List[BenchObservation]) -> bool:
        if len(rows) < 2:
            return True
        first = rows[0]
        for other in rows[1:]:
            if (other.status != first.status or other.ok != first.ok
                    or _canonical(other.value) != _canonical(first.value)):
                return False
        return True

    # ---- 对照与结论 ----
    def compare(self, baseline_ref: Any, candidate_ref: Any,
                cases: List[BenchCase]) -> Dict[str, Any]:
        if not cases:
            raise BenchError("基准至少需要一个用例")
        baseline_identity = self.host.resolve(baseline_ref).identity()
        candidate_identity = self.host.resolve(candidate_ref).identity()
        rows: List[Dict[str, Any]] = []
        omitted: List[Dict[str, str]] = []
        regression_cases: List[str] = []
        violations = 0
        reproducible = True
        base_pass = cand_pass = covered = 0
        base_times: List[float] = []
        cand_times: List[float] = []
        domains: Dict[str, Dict[str, int]] = {}
        for case in cases:
            bucket = domains.setdefault(case.domain,
                                        {"total": 0, "covered": 0, "required": 0})
            bucket["total"] += 1
            if case.required:
                bucket["required"] += 1
            base_rows = self.observe(baseline_ref, case)
            cand_rows = self.observe(candidate_ref, case)
            row = {"case_id": case.case_id, "domain": case.domain,
                   "required": case.required,
                   "baseline": base_rows[0].to_dict(),
                   "candidate": cand_rows[0].to_dict()}
            base_det = self._deterministic(base_rows)
            cand_det = self._deterministic(cand_rows)
            row["reproducible"] = bool(base_det and cand_det)
            if not row["reproducible"]:
                reproducible = False
                omitted.append({"case_id": case.case_id,
                                "reason": "non_deterministic"})
            for rows_side, name in ((base_rows, "baseline"),
                                    (cand_rows, "candidate")):
                if rows_side[0].status in ("timeout", "violation", "invalid_output"):
                    if case.required:
                        omitted.append({"case_id": case.case_id,
                                        "reason": f"{name}:{rows_side[0].status}"})
                    row.setdefault("omissions", []).append(
                        {"side": name, "reason": rows_side[0].status})
            if cand_rows[0].status == "violation":
                violations += 1
            base_ok = base_rows[0].ok and base_rows[0].status == "ok"
            cand_ok = cand_rows[0].ok and cand_rows[0].status == "ok"
            # 覆盖 = 两侧都真实执行且输出可解析（成功/失败都算覆盖，成败进成功率）
            if (base_rows[0].status in ("ok", "failed")
                    and cand_rows[0].status in ("ok", "failed")):
                covered += 1
                bucket["covered"] += 1
            if base_ok:
                base_pass += 1
            if cand_ok:
                cand_pass += 1
            # 只有候选正常执行（非超时/越界/崩溃）且由通过变失败才算依赖回归
            if base_ok and not cand_ok and cand_rows[0].status in ("ok", "failed"):
                regression_cases.append(case.case_id)
            base_times.append(base_rows[0].duration_s)
            cand_times.append(cand_rows[0].duration_s)
            rows.append(row)
        total = len(cases)
        required_total = sum(1 for case in cases if case.required)
        coverage_ratio = covered / total if total else 0.0
        coverage = {"total": total, "required": required_total,
                    "covered": covered,
                    "coverage_ratio": round(coverage_ratio, 4),
                    "required_coverage_ok": not any(
                        item["case_id"] in {case.case_id for case in cases if case.required}
                        for item in omitted),
                    "omitted": omitted,
                    "domains": domains}
        base_rate = base_pass / total if total else 0.0
        cand_rate = cand_pass / total if total else 0.0
        base_median = statistics.median(base_times) if base_times else 0.0
        cand_median = statistics.median(cand_times) if cand_times else 0.0
        latency_delta = ((cand_median - base_median) / base_median
                         if base_median > 0 else 0.0)
        metrics = {"baseline_success_rate": round(base_rate, 4),
                   "candidate_success_rate": round(cand_rate, 4),
                   "success_delta": round(cand_rate - base_rate, 4),
                   "baseline_latency_median_s": round(base_median, 4),
                   "candidate_latency_median_s": round(cand_median, 4),
                   "latency_delta_ratio": round(latency_delta, 4),
                   "violations": violations}
        cases_digest = hashlib.sha256(_canonical(
            [{"case_id": case.case_id, "input": case.input,
              "domain": case.domain, "required": case.required} for case in cases]
        ).encode()).hexdigest()
        report: Dict[str, Any] = {
            "benchmark_version": BENCH_VERSION,
            "baseline": baseline_identity,
            "candidate": candidate_identity,
            "repeat": self.repeat,
            "timeout_s": self.timeout,
            "cases_digest": cases_digest,
            "source": self.source,
            "reproducible": reproducible,
            "coverage": coverage,
            "metrics": metrics,
            "regression_cases": regression_cases,
            "cases": rows,
        }
        report["benefit"] = self._benefit(metrics, regression_cases,
                                          reproducible, coverage)
        report["verdict"] = report["benefit"]["verdict"]
        report["evidence_digest"] = evidence_digest(report)
        report["release_decision"] = release_decision(report)
        return report

    def _benefit(self, metrics: Dict[str, Any], regression_cases: List[str],
                 reproducible: bool, coverage: Dict[str, Any]) -> Dict[str, Any]:
        blockers: List[str] = []
        if not reproducible:
            blockers.append("对照不可复现")
        if regression_cases:
            blockers.append("候选回归: " + ", ".join(regression_cases))
        if metrics["violations"] > self.max_violations:
            blockers.append("候选执行越界违规: %d" % metrics["violations"])
        if not coverage["required_coverage_ok"]:
            blockers.append("必跑用例覆盖不完整")
        if coverage["coverage_ratio"] < self.min_coverage:
            blockers.append("覆盖率低于阈值 %.2f" % self.min_coverage)
        if blockers:
            verdict = "regressed" if regression_cases else "inconclusive"
        elif metrics["success_delta"] > 0 and (
                metrics["latency_delta_ratio"] <= self.latency_tolerance):
            verdict = "improved"
        else:
            verdict = "neutral"
        return {"verdict": verdict,
                "success_delta": metrics["success_delta"],
                "latency_delta_ratio": metrics["latency_delta_ratio"],
                "blockers": blockers,
                "claim_scope": "benchmark-only"}   # 不宣称线上流量收益


def release_decision(report: Dict[str, Any]) -> Dict[str, Any]:
    """把证据翻译成发布门禁输入（fail-closed：任何阻断项都不放行）。"""
    blockers: List[str] = []
    if not report.get("reproducible"):
        blockers.append("对照不可复现")
    if report.get("regression_cases"):
        blockers.append("候选在必跑用例上回归")
    benefit = report.get("benefit") or {}
    blockers.extend(benefit.get("blockers") or [])
    if report.get("verdict") == "inconclusive":
        blockers.append("收益证据不足（inconclusive）")
    seen = list(dict.fromkeys(blockers))
    return {"allow": not seen, "blockers": seen,
            "verdict": report.get("verdict"),
            "evidence_digest": report.get("evidence_digest")}


def benefit_chain(report: Dict[str, Any],
                  post_release: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """发布前后收益链：发布前对照结论 + 发布后监测，缺一不可。

    ``post_release`` 为空时明确标记 ``pending``（不得当作已验证收益）；
    发布后出现退化则给出 ``rollback_required=True``（既定回滚触发点）。
    """
    pre = {"verdict": report.get("verdict"),
           "metrics": report.get("metrics"),
           "coverage": report.get("coverage"),
           "evidence_digest": report.get("evidence_digest"),
           "baseline": report.get("baseline"),
           "candidate": report.get("candidate")}
    chain: Dict[str, Any] = {"pre_release": pre, "post_release": None,
                             "status": "pending",
                             "rollback_required": False}
    if not post_release:
        chain["note"] = "缺少发布后监测证据，收益链未闭合（不得宣称线上收益）"
        return chain
    observed_rate = post_release.get("success_rate")
    degraded = bool(post_release.get("regression")) or (
        observed_rate is not None
        and report.get("metrics", {}).get("baseline_success_rate") is not None
        and observed_rate < report["metrics"]["baseline_success_rate"])
    chain.update(post_release=post_release, status="complete",
                 rollback_required=degraded)
    if degraded:
        chain["rollback_reason"] = "发布后监测退化"
    return chain
