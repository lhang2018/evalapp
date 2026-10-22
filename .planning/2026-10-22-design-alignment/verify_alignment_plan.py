"""Check coverage/dependencies of audit docs, not implementation correctness."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docs/EvoAgent_全量详细设计正式版_1.0/EvoAgent_全量详细设计正式版"
PLAN = ROOT / "docs/全量对齐与下一步实施计划_2026-10-22.md"
REPORT = ROOT / "docs/项目现状与设计差距核对_2026-10-22.md"


def main():
    text = PLAN.read_text(encoding="utf-8")
    report = REPORT.read_text(encoding="utf-8")
    data = json.loads((Path(__file__).parent / "implementation-workpackages.json").read_text(encoding="utf-8"))
    topics = set(re.findall(r"\*\*([PRMCEGOS]-\d{2})｜", (BASE / "00_总纲与总体架构/EvoAgent_全量目录与追踪矩阵.md").read_text(encoding="utf-8")))
    spec_entities = set(re.findall(r"^### ((?:A|SV|CTRL)-\d{2})｜", (BASE / "07_端云协同离线同步与发布/EvoAgent_Android服务端协同与控制器详细设计.md").read_text(encoding="utf-8"), re.M))
    spec_adrs = set(re.findall(r"^## (ADR-\d{3})：", (BASE / "10_术语与架构决策/EvoAgent_ADR架构决策正式台账.md").read_text(encoding="utf-8"), re.M))
    plan_topics = re.findall(r"^\| ([PRMCEGOS]-\d{2}) \|", text, re.M)
    report_topics = re.findall(r"^\| ([PRMCEGOS]-\d{2}) \|", report, re.M)
    plan_entities = re.findall(r"^\| ((?:A|SV|CTRL)-\d{2}) \|", text, re.M)
    plan_adrs = re.findall(r"^\| (ADR-\d{3}) \|", text, re.M)
    errors = []

    def check_ids(label, values, expected):
        if len(values) != len(set(values)):
            errors.append(f"{label}: duplicate IDs")
        if set(values) != expected:
            errors.append(f"{label}: missing={sorted(expected-set(values))}, extra={sorted(set(values)-expected)}")

    check_ids("plan topics", plan_topics, topics)
    check_ids("report topics", report_topics, topics)
    check_ids("entities", plan_entities, spec_entities)
    check_ids("ADRs", plan_adrs, spec_adrs)
    if len(topics) != 89 or len(spec_entities) != 19 or len(spec_adrs) != 14:
        errors.append("formal baseline size changed; re-audit required")

    contract_section = text.split("## 8.", 1)[1].split("## 9.", 1)[0]
    acceptance_section = text.split("## 9.", 1)[1].split("## 10.", 1)[0]
    contract_nums = [int(x) for x in re.findall(r"^\| (\d+) \|", contract_section, re.M)]
    acceptance_nums = [int(x) for x in re.findall(r"^\| (\d+) \|", acceptance_section, re.M)]
    check_ids("contract sections", contract_nums, set(range(1, 12)))
    check_ids("acceptance cases", acceptance_nums, set(range(1, 21)))

    packages = {p[0]: p for p in data["workpackages"]}
    headers = re.findall(r"^### (ALN-\d{2}) ", text, re.M)
    check_ids("workpackage sections", headers, set(packages))
    for package_id in re.findall(r"ALN-\d{2}", text):
        if package_id not in packages:
            errors.append(f"unknown workpackage {package_id}")
    if set(data["topic_primary"]) != topics:
        errors.append("machine mapping topic mismatch")
    for topic, package in data["topic_primary"].items():
        if package not in packages:
            errors.append(f"unknown primary package {topic}: {package}")
        row = re.search(rf"^\| {re.escape(topic)} \|.*$", text, re.M)
        if not row or not row.group(0).endswith(f"| {package} |"):
            errors.append(f"document primary mapping differs: {topic}")
    active, done = set(), set()

    def visit(package_id):
        if package_id in active:
            errors.append(f"dependency cycle at {package_id}")
            return
        if package_id in done:
            return
        active.add(package_id)
        package = packages[package_id]
        if not all(package[i] for i in (4, 5, 6, 7)):
            errors.append(f"missing workpackage scope/exit criteria: {package_id}")
        for dep in package[3]:
            if dep not in packages:
                errors.append(f"unknown dependency {package_id}: {dep}")
            else:
                visit(dep)
        active.remove(package_id)
        done.add(package_id)

    for package_id in packages:
        visit(package_id)
    result = {"scope": "documentation coverage only", "topics": len(plan_topics), "entities": len(plan_entities), "adrs": len(plan_adrs), "contract_sections": len(contract_nums), "acceptance_cases": len(acceptance_nums), "workpackages": len(packages), "errors": errors, "ok": not errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
