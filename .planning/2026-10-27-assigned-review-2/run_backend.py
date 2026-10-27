"""Run the union of modules mapped to the eleven current codex review cards."""
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
cards = ["ALN-18-L", "ALN-19-A", "ALN-30-B", "ALN-16-Q", "ALN-15-E", "ALN-13-S",
         "ALN-09-I", "ALN-18-H", "ALN-25-C", "ALN-27-C", "ALN-24-F"]
coverage = json.loads((root / ".planning/2026-10-26-assigned-review/coverage.json").read_text(encoding="utf-8"))
mapping = {card: list(coverage["cards"][card]) for card in cards}
for card in ("ALN-18-L", "ALN-24-F"):
    mapping[card].append("test_assigned_review_20261027")
modules = sorted({module for group in mapping.values() for module in group})
modules.append("test_runtime_admission_review_20261026")
for module in modules:
    assert (root / "tests" / (module + ".py")).is_file(), module
source = subprocess.check_output(["git", "rev-parse", sys.argv[1] + "^{commit}"], cwd=root, text=True).strip()
args = [sys.executable, "-X", "utf8", str(root / ".planning/2026-10-25-developed-review-5/run_scoped_pytest.py"),
        "--record", str((folder / "final-validation.json").relative_to(root)), "--snapshot", source,
        *["tests/" + module + ".py" for module in modules], "-q", "-k",
        "not test_real_host_pressure_yields_and_core_state_untouched", "--junitxml=" + str(folder / "junit.xml")]
(folder / "coverage.json").write_text(json.dumps({"commit": source, "cards": mapping, "modules": modules,
    "extra_shared_module": "test_runtime_admission_review_20261026",
    "note": "Overlapping modules and combined 18-L/24-F probes: per-card counts are not additive."}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
with (folder / "final-tests.txt").open("w", encoding="utf-8") as output:
    result = subprocess.run(args, cwd=root, stdout=output, stderr=subprocess.STDOUT)
raise SystemExit(result.returncode)
