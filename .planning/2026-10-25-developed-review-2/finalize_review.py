"""Finalize second-batch review evidence after the diagnostic run completes."""
import hashlib,json,re,subprocess
from pathlib import Path
folder=Path(__file__).resolve().parent
root=folder.parents[1]
first=json.loads((folder/"fixed-source.json").read_text(encoding="utf-8"))
rerun=(folder/"fixed-full-python-diagnostic-rerun.txt").read_text(encoding="utf-8")
summary=re.findall(r"(?:\d+ failed, )?\d+ passed, \d+ skipped[^\n]*",rerun)
assert summary, "diagnostic full run is still incomplete"
logs=[]
for path in sorted(folder.glob("*.txt")):
    data=path.read_bytes().replace(b"\r\n",b"\n")
    logs.append({"path":path.relative_to(root).as_posix(),"sha256_lf":hashlib.sha256(data).hexdigest()})
for name in ["fixed-source.json","targeted-source.json","claims-check.json","ledgers-check.json","migration_failure_diagnostic.py"]:
    path=folder/name
    logs.append({"path":path.relative_to(root).as_posix(),"sha256_lf":hashlib.sha256(path.read_bytes().replace(b"\r\n",b"\n")).hexdigest()})
evidence={"runtime_fix_commit":"0cf66722d00ab59f1405c6bca7f322c1bf893a9c",
"followup_commit":"df29a2e200d085901404bccf1dc739e9d3b01ef5","snapshot":first,
"fixed_full_backend_initial":{"passed":2059,"failed":1,"skipped":1,
"failed_test":"tests/test_model_migration.py::TestMigrationApi::test_migration_chain_via_api",
"observed":"regression returned blocked instead of evaluated; initial result retained"},
"fixed_full_backend_diagnostic_rerun":{"summary":summary[-1].strip(),
"scope":"same exact 0cf6672 archive; added observation-only pytest failure diagnostic plugin, no product source changes"},
"migration_followup":{"related_tests_passed":36,"isolated_api_chain_repeats_passed":4,
"cause":"unconfirmed; no policy threshold loosened and no test deleted"},
"fixed_client_passed":328,"fixed_typecheck":"passed","typescript":"5.9.3",
"dependencies":"existing installed node_modules reused through junction; no npm ci claim",
"followup_targeted_passed":108,"followup_scope":"7 relevant Python test modules; exact owned source/test blobs matched df29a2e",
"product_acceptance_added":False,"device_tested_for_new_code":False,
"independent_review_of_new_fixes":False,"published_by_this_review":False,"logs":logs}
(folder/"committed-evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
print(summary[-1].strip())
