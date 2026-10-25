"""Exercise discovery against real isolated Git history, including same-file edits."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("coverage_checker", root / "tools/check_source_coverage.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
fixture = Path(tempfile.mkdtemp(prefix="coverage-smoke-", dir=root / "outputs")).resolve()
assert fixture.is_relative_to(root / "outputs")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=fixture, stderr=subprocess.DEVNULL).decode().strip()


git("init")
git("config", "user.name", "Coverage fixture")
git("config", "user.email", "fixture@example.invalid")
git("config", "core.autocrlf", "false")
(fixture / "evoagent").mkdir()
source = fixture / "evoagent/demo.py"
source.write_text("one\n", encoding="utf-8")
git("add", "evoagent/demo.py")
git("commit", "-m", "base")
base = git("rev-parse", "HEAD")
board_path = fixture / "docs/协作开发/workboard.json"
board_path.parent.mkdir(parents=True)
board = dict(baseline=dict(prepared_candidate=dict(commit=base)), dispatches=[])


def save():
    board_path.write_text(json.dumps(board), encoding="utf-8")


def edit(text):
    source.write_text(text + "\n", encoding="utf-8")
    git("add", "evoagent/demo.py")
    git("commit", "-m", text)
    return git("rev-parse", "HEAD")


save()
first = edit("two")
assert checker.check(fixture)["unregistered_files"] == ["evoagent/demo.py"]
board["post_candidate_changes"] = [dict(id="first", source_commit=first, files=["evoagent/demo.py"])]
save()
assert checker.check(fixture)["ok"]
cards = fixture / "docs/协作开发/任务卡"
cards.mkdir()
card = cards / "新增中文任务.md"
card.write_text("fixture", encoding="utf-8")
assert checker.check(fixture)["unregistered_task_cards"] == ["docs/协作开发/任务卡/新增中文任务.md"]
board["dispatches"] = [dict(card="docs/协作开发/任务卡/新增中文任务.md")]
save()
assert checker.check(fixture)["ok"]
later = edit("three")
assert checker.check(fixture)["unregistered_files"] == ["evoagent/demo.py"]
board["post_candidate_changes"].append(dict(id="later", source_commit=later, files=["evoagent/demo.py"]))
save()
assert checker.check(fixture)["ok"]
board["post_candidate_changes"][-1]["files"].append("evoagent/not-in-commit.py")
save()
assert not checker.check(fixture)["ok"]
board["post_candidate_changes"][-1]["files"].pop()
save()
git("add", "docs")
git("commit", "-m", "fixture metadata")
metadata = git("rev-parse", "HEAD")
archive_path = fixture / "archive"
archive_path.mkdir()
with zipfile.ZipFile(io.BytesIO(subprocess.check_output(["git", "archive", "--format=zip", metadata], cwd=fixture))) as archive:
    archive.extractall(archive_path)
assert checker.check(archive_path, metadata)["ok"]
result = dict(passed=8, scenarios=["new source found", "registered source accepted", "new Chinese task card found",
    "registered card accepted", "later same-file edit found", "latest file version accepted", "false file claim rejected",
    "archive files checked against backing Git objects"],
    fixture=str(fixture), scope="real isolated Git fixture; no product behavior test")
(Path(__file__).parent / "coverage-smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8", newline="\n")
print(json.dumps(result, indent=2))
