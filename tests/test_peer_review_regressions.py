"""Independent negative probes for other authors' delivery boundaries."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from evoagent.planning.planner import ExecutionPlan, PlanStep, PlanRevisionError
from evoagent.tools import coding


@pytest.mark.parametrize("level", ["append", "replace", "replan"])
def test_completed_step_cannot_be_relabelled_as_another_completed_action(level):
    step = PlanStep("done", "read approved document", tool_hint="read", state="done")
    plan = ExecutionPlan("p", "goal", [step], completed_work=["done"])
    before = copy.deepcopy(plan.to_dict())
    changed = PlanStep("done", "delete all documents", tool_hint="delete", state="done")
    with pytest.raises(PlanRevisionError):
        plan.adjust([changed], "keep ID but change meaning", level=level)
    assert plan.to_dict() == before


def test_append_cannot_modify_existing_pending_or_running_steps():
    plan = ExecutionPlan("p", "goal", [PlanStep("s", "safe", state="running")])
    with pytest.raises(PlanRevisionError):
        plan.adjust([PlanStep("s", "different", state="pending")], "append", level="append")


def test_revision_does_not_keep_mutable_caller_step_aliases():
    plan = ExecutionPlan("p", "goal", [PlanStep("s", "safe")])
    proposed = PlanStep("s", "replacement")
    plan.adjust([proposed], "replace")
    proposed.description = "changed after validation"
    assert plan.steps[0].description == "replacement"


def test_patch_failed_commit_marker_is_not_reported_as_success(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_text("before\n", encoding="utf-8")
    def fail(*_):
        raise OSError("disk full while finalizing journal")
    monkeypatch.setattr(coding, "_mark_committed", fail)
    result = coding.coding_patch_tool(str(tmp_path))[1]({"edits": [
        {"path":"file.txt", "old_text":"before", "new_text":"after"}]})
    assert not result.ok
    assert result.side_effect_uncertain or result.evidence.get("rolled_back")


def test_recovery_entry_error_cannot_report_full_success(tmp_path):
    journal = tmp_path / coding._JOURNAL_DIR
    journal.mkdir()
    (journal / "bad.json").write_text(json.dumps({"version":1, "status":"pending", "files":[{
        "path":"file.txt", "sha256_before":"wrong", "sha256_after":"after", "original":"before"}]}), encoding="utf-8")
    result = coding.coding_patch_recover_tool(str(tmp_path))[1]({})
    assert not result.ok
    assert (journal / "bad.json").exists()


def test_patch_rechecks_snapshot_after_preflight_before_writing(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_text("before\n", encoding="utf-8")
    original = coding._write_journal
    def race(*args):
        result = original(*args)
        path.write_text("other editor\n", encoding="utf-8")
        return result
    monkeypatch.setattr(coding, "_write_journal", race)
    result = coding.coding_patch_tool(str(tmp_path))[1]({"edits":[{
        "path":"file.txt", "old_text":"before", "new_text":"after",
        "expected_sha256":hashlib.sha256(b"before\n").hexdigest()}]})
    assert not result.ok
    assert path.read_text(encoding="utf-8") == "other editor\n"


def test_patch_new_file_cannot_overwrite_file_created_after_preflight(tmp_path, monkeypatch):
    original = coding._write_journal
    path = tmp_path / "new.txt"
    def race(*args):
        result = original(*args)
        path.write_text("other writer\n", encoding="utf-8")
        return result
    monkeypatch.setattr(coding, "_write_journal", race)
    result = coding.coding_patch_tool(str(tmp_path))[1]({"diff":
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+proposed\n"})
    assert not result.ok
    assert path.read_text(encoding="utf-8") == "other writer\n"


def test_rollback_does_not_overwrite_concurrent_foreign_file(tmp_path, monkeypatch):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("one\n", encoding="utf-8")
    b.write_text("two\n", encoding="utf-8")
    original = Path.write_text
    def fail(path, content, *args, **kwargs):
        if path == b and content == "changed two\n":
            original(b, "foreign content\n", encoding="utf-8")
            raise OSError("concurrent write and failed commit")
        return original(path, content, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail)
    result = coding.coding_patch_tool(str(tmp_path))[1]({"edits":[
        {"path":"a.txt", "old_text":"one", "new_text":"changed one"},
        {"path":"b.txt", "old_text":"two", "new_text":"changed two"}]})
    assert not result.ok
    assert b.read_text(encoding="utf-8") == "foreign content\n"


def test_unified_hunk_cannot_match_middle_of_an_unrelated_line(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("prefix needle\n", encoding="utf-8")
    result = coding.coding_patch_tool(str(tmp_path))[1]({"diff":
        "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-needle\n+replacement\n"})
    assert not result.ok
    assert path.read_text(encoding="utf-8") == "prefix needle\n"


def test_journal_directory_cannot_escape_project_through_link(tmp_path):
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir(); outside.mkdir()
    link = root / coding._JOURNAL_DIR
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlinks unavailable")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if result.returncode:
            pytest.skip("directory junction unavailable")
    target = root / "file.txt"
    target.write_text("before\n", encoding="utf-8")
    result = coding.coding_patch_tool(str(root))[1]({"edits":[{
        "path":"file.txt", "old_text":"before", "new_text":"after"}]})
    assert not result.ok
    assert not list(outside.iterdir())
    assert target.read_text(encoding="utf-8") == "before\n"


def test_signing_key_identity_cannot_be_rebound_to_a_different_secret():
    from evoagent.release.signing import KeyRing, SigningError
    keys = KeyRing()
    keys.add("historical", "original-secret")
    with pytest.raises((ValueError, SigningError)):
        keys.add("historical", "replacement-secret")
    assert keys.get("historical").secret == "original-secret"


def test_deleted_memory_identity_cannot_be_resurrected(tmp_path):
    from evoagent.memory.store import MemoryStore, MemoryItem
    store = MemoryStore(str(tmp_path / "memory.db"))
    try:
        item = store.write(MemoryItem("sensitive fact"))
        store.delete(item.memory_id, actor="user", reason="erase")
        with pytest.raises((ValueError, PermissionError)):
            store.write(MemoryItem("sensitive fact", memory_id=item.memory_id))
        assert store.get(item.memory_id) is None
    finally:
        store.close()


def test_late_cached_memory_cannot_reintroduce_deleted_source(tmp_path):
    from evoagent.memory.store import MemoryStore, MemoryItem
    store = MemoryStore(str(tmp_path / "memory.db"))
    try:
        source = store.write(MemoryItem("private source"))
        store.delete(source.memory_id, actor="user", reason="erase")
        with pytest.raises((ValueError, PermissionError)):
            store.write(MemoryItem("late cached private source", relations=[{
                "relation":"cached_from", "target":source.memory_id}]))
    finally:
        store.close()


def test_duplicate_memory_write_returns_a_real_persisted_identity(tmp_path):
    from evoagent.memory.store import MemoryStore, MemoryItem
    store = MemoryStore(str(tmp_path / "memory.db"))
    try:
        first = store.write(MemoryItem("same fact"))
        replay = store.write(MemoryItem("same fact"))
        assert replay.memory_id == first.memory_id
        assert store.get(replay.memory_id) is not None
    finally:
        store.close()


def test_identity_scoped_write_cannot_overwrite_another_users_memory_id(tmp_path):
    from evoagent.memory.store import MemoryStore, MemoryItem, Scope
    from evoagent.memory.manifest import MemoryIdentity
    store = MemoryStore(str(tmp_path / "memory.db"))
    try:
        original = store.write(MemoryItem("Alice private", scope=Scope.USER), identity=MemoryIdentity(user_id="alice"))
        with pytest.raises(PermissionError):
            store.write(MemoryItem("Bob replacement", scope=Scope.USER, memory_id=original.memory_id),
                        identity=MemoryIdentity(user_id="bob"))
        assert store.get(original.memory_id).content == "Alice private"
    finally:
        store.close()


@pytest.mark.parametrize("new_request_id", [False, True])
def test_metering_does_not_reuse_provider_usage_from_the_previous_response(new_request_id):
    from evoagent.models.metering import MeteredModel, UsageLedger
    from evoagent.models.base import ModelProfile, ChatMessage
    class Provider:
        last_usage = None
        calls = 0
        def profile(self):
            return ModelProfile("probe", pricing_known=True, price_per_1k_input=1, price_per_1k_output=1)
        def generate(self, messages):
            self.calls += 1
            if new_request_id:
                self.last_request_id = str(self.calls)
            if self.calls == 1:
                self.last_usage = {"input_tokens":10, "output_tokens":10}
            return "no usage in second response"
    ledger = UsageLedger()
    metered = MeteredModel(Provider(), ledger=ledger)
    for _ in range(2):
        metered.generate([ChatMessage("user", "hello")])
    assert metered.last_usage.cost_source != "provider"
    assert ledger.totals()["actual_cost"] == pytest.approx(.02)


def test_typechecker_is_not_declared_a_side_effect_free_preparation_action(tmp_path):
    contract, _ = coding.coding_typecheck_tool(str(tmp_path))
    assert contract.side_effect_level == "external"
    assert contract.risk_class == "high"


@pytest.mark.parametrize("replacement", [{"value":"original"}, {"value":"replacement"}])
def test_asset_collected_identity_remains_reserved_across_restart(tmp_path, replacement):
    import sqlite3
    from evoagent.assets.repository import AssetRepository
    from evoagent.assets.entity import AssetConflict
    path = str(tmp_path / "assets.db")
    conn = sqlite3.connect(path)
    repo = AssetRepository(conn)
    fields = dict(asset_id="peer:asset", asset_type="tool", namespace="shared_validated", version="1")
    repo.register(**fields, content={"value":"original"})
    repo.transition("peer:asset", "1", "retired")
    assert repo.collect("peer:asset", "1")
    conn.close()
    conn = sqlite3.connect(path)
    try:
        repo = AssetRepository(conn)
        with pytest.raises(AssetConflict):
            repo.register(**fields, content=replacement)
        assert repo.get("peer:asset", "1") is None
    finally:
        conn.close()


def test_asset_activation_rejects_missing_evaluation_evidence(tmp_path):
    import sqlite3
    from evoagent.assets.repository import AssetRepository
    from evoagent.assets.entity import LifecycleError
    conn = sqlite3.connect(str(tmp_path / "assets.db"))
    try:
        repo = AssetRepository(conn)
        repo.register(asset_id="peer:asset", asset_type="tool", namespace="shared_validated", version="1", content={"value":1})
        repo.transition("peer:asset", "1", "candidate")
        repo.transition("peer:asset", "1", "validating")
        with pytest.raises(LifecycleError):
            repo.transition("peer:asset", "1", "approved")
        assert repo.get("peer:asset", "1").lifecycle_state == "validating"
    finally:
        conn.close()


def test_live_patch_excludes_another_cooperating_editor(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_text("before\n", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    original = coding._write_journal
    def hold(*args):
        journal = original(*args)
        entered.set()
        assert release.wait(5)
        return journal
    monkeypatch.setattr(coding, "_write_journal", hold)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(coding.coding_patch_tool(str(tmp_path))[1], {"edits":[{
            "path":"file.txt", "old_text":"before", "new_text":"after"}]})
        try:
            assert entered.wait(3)
            other = coding.coding_edit_tool(str(tmp_path))[1]({"path":"file.txt", "old_text":"before", "new_text":"racing"})
            assert not other.ok
        finally:
            release.set()
        assert running.result().ok
    assert path.read_text(encoding="utf-8") == "after\n"


def test_malformed_recovery_payload_is_reported_without_crashing(tmp_path):
    journal = tmp_path / coding._JOURNAL_DIR
    journal.mkdir()
    (journal / "bad.json").write_text("[]", encoding="utf-8")
    result = coding.coding_patch_recover_tool(str(tmp_path))[1]({})
    assert not result.ok and result.data["errors"]


def test_single_file_partial_write_error_is_reported_as_uncertain(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_text("before\n", encoding="utf-8")
    original = Path.write_text
    def fail(target, text, *args, **kwargs):
        if target == path:
            original(target, "partial\n", *args, **kwargs)
            raise OSError("partial write then disk error")
        return original(target, text, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail)
    result = coding.coding_edit_tool(str(tmp_path))[1]({"path":"file.txt", "old_text":"before", "new_text":"after"})
    assert not result.ok and result.side_effect_uncertain
    assert result.executed
