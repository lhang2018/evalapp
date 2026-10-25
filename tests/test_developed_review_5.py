"""Negative security/refactor probes and bounded 07-C rechecks."""

import ast
import json

import pytest

from evoagent.core.persistence import Database
from evoagent.observability.credentials import (
    CredentialLeakError,
    assert_context_safe,
    contains_secret,
    scrub_text,
    scrub_value,
)
from evoagent.observability.feedback import FeedbackError, FeedbackStore
from evoagent.planning.binding import ActionBindings
from evoagent.tools.refactor import analyze_rename


@pytest.mark.parametrize(
    "text",
    [
        '{"api_key": "review-only-123456"}',
        r"{\"password\": \"review only password\"}",
        "password=x",
        "Authorization: Basic cmV2aWV3OnNlY3JldA==",
        "github_pat_" + "x" * 30,
    ],
)
def test_secret_text_is_detected_and_fully_redacted(text):
    assert contains_secret(text)
    cleaned = scrub_text(text)
    assert "review-only-123456" not in cleaned
    assert "review only password" not in cleaned
    assert "cmV2aWV3OnNlY3JldA" not in cleaned
    assert "x" * 30 not in cleaned
    assert not contains_secret(cleaned)


def test_nested_credential_json_is_not_treated_as_ordinary_text():
    text = '{"credentials": {"value": "review-private-value"}}'
    assert contains_secret(text)
    assert "review-private-value" not in scrub_text(text)
    assert not contains_secret(scrub_text(text))
    assert not contains_secret('{"api_key": null, "token_count": 5}')


@pytest.mark.parametrize(
    "payload",
    [
        {"sk-review-123456789": "value"},
        {"api-key": "review-only"},
        {"privateKey": "review-only"},
        {"nested": {"Bearer reviewSecret123456"}},
    ],
)
def test_context_gate_covers_keys_aliases_and_sets(payload):
    with pytest.raises(CredentialLeakError) as exc:
        assert_context_safe(payload)
    assert "123456789" not in str(exc.value)


def test_export_redacts_sensitive_structured_values():
    cleaned = scrub_value(
        {
            "password": "plain short value",
            "api-key": "x",
            "nested": {"Bearer reviewSecret123456"},
        }
    )
    text = json.dumps(cleaned)
    assert "plain short value" not in text
    assert "reviewSecret123456" not in text
    assert cleaned["api-key"] != "x"


def test_feedback_cannot_persist_embedded_json_credentials():
    db = Database(":memory:")
    try:
        with pytest.raises(FeedbackError):
            FeedbackStore(db._conn).submit(
                category="bug", text='{"api_key": "review-only-value"}'
            )
        assert db._conn.execute("SELECT COUNT(*) FROM user_feedback").fetchone()[0] == 0
    finally:
        db.close()


def project(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("name", ["123bad", "class", "two names"])
def test_invalid_rename_target_never_returns_executable_plan(tmp_path, name):
    project(tmp_path, {"a.py": "def helper():\n    return 1\n"})
    result = analyze_rename(tmp_path, "helper", name)
    assert result.status == "needs_clarification" and not result.edits


@pytest.mark.parametrize(
    "extra",
    [
        "def other(helper):\n    return helper\n",
        "def other(obj):\n    return obj.helper\n",
        "helper = 2\n",
    ],
)
def test_shadowed_or_unresolved_identity_needs_clarification(tmp_path, extra):
    project(tmp_path, {"a.py": "def helper():\n    return 1\n\n" + extra})
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


def test_foreign_import_is_not_silently_renamed(tmp_path):
    project(
        tmp_path,
        {
            "a.py": "def helper():\n    return 1\n",
            "b.py": "from external_library import helper\nvalue = helper()\n",
        },
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


def test_no_definition_cannot_prove_rename_identity(tmp_path):
    project(tmp_path, {"a.py": "value = helper()\n"})
    assert analyze_rename(tmp_path, "helper", "compute").status == "needs_clarification"


def test_fstring_expression_is_updated_or_explicitly_refused(tmp_path):
    project(
        tmp_path,
        {"a.py": "def helper():\n    return 1\n" + 'message = f"{helper()}"\n'},
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    if result.status == "needs_clarification":
        assert not result.edits
    else:
        source = result.edits[0]["new_text"]
        namespace = {}
        exec(compile(source, "a.py", "exec"), namespace)
        assert namespace["message"] == "1"


def test_non_utf8_source_does_not_crash_refactor(tmp_path):
    (tmp_path / "a.py").write_bytes(b"def helper():\n    return 1\n")
    (tmp_path / "b.py").write_bytes(b"# bad utf8 \xff\n")
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


def test_external_file_link_is_never_read_into_plan(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "private.py"
    outside.write_text("def helper():\n    return 'PRIVATE_SOURCE'\n", encoding="utf-8")
    try:
        (root / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlink unavailable")
    result = analyze_rename(root, "helper", "compute")
    assert result.status == "needs_clarification"
    assert "PRIVATE_SOURCE" not in str(result.edits)


def test_refactor_excludes_dependency_tree(tmp_path):
    project(
        tmp_path,
        {
            "a.py": "def helper():\n    return 1\n",
            "node_modules/dependency/a.py": "def helper():\n    return 2\n",
        },
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "ok"
    assert [r["path"] for r in result.edits] == ["a.py"]


def test_well_scoped_cross_file_plan_remains_valid(tmp_path):
    project(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper():\n    return 1\n",
            "pkg/main.py": "from .util import helper as alias\nvalue = alias()\n",
        },
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "ok", result.questions
    assert len(result.edits) == 2
    for edit in result.edits:
        ast.parse(edit["new_text"])
    main = next(e["new_text"] for e in result.edits if e["path"] == "pkg/main.py")
    assert "import compute as alias" in main and "alias()" in main


def test_crlf_rename_plan_is_consumed_by_real_patch_guard(tmp_path):
    from evoagent.tools.coding import coding_patch_tool

    (tmp_path / "a.py").write_bytes(
        b"def helper():\r\n    return 1\r\nvalue = helper()\r\n"
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "ok", result.questions
    _, patch = coding_patch_tool(str(tmp_path))
    applied = patch({"edits": list(result.edits)})
    assert applied.ok, applied.error
    namespace = {}
    exec((tmp_path / "a.py").read_text(encoding="utf-8"), namespace)
    assert namespace["value"] == 1


def test_proven_module_alias_reference_can_be_renamed(tmp_path):
    project(
        tmp_path,
        {
            "util.py": "def helper():\n    return 1\n",
            "main.py": "import util as u\nvalue = u.helper()\n",
        },
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "ok", result.questions
    assert any("u.compute()" in r["new_text"] for r in result.edits)


def test_rebound_module_alias_is_not_trusted(tmp_path):
    project(
        tmp_path,
        {
            "util.py": "def helper():\n    return 1\n",
            "main.py": "import util as u\nu = object()\nvalue = u.helper\n",
        },
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


def test_explicit_export_and_forward_annotation_require_clarification(tmp_path):
    project(
        tmp_path,
        {"util.py": "__all__ = ['helper']\nclass helper:\n    pass\nvalue: 'helper'\n"},
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


def test_compile_level_diagnostics_catch_invalid_top_level_return(tmp_path):
    from evoagent.tools.refactor import collect_diagnostics

    project(tmp_path, {"bad.py": "return 1\n"})
    assert collect_diagnostics(tmp_path)


def test_dynamic_module_loading_needs_explicit_scope(tmp_path):
    project(
        tmp_path,
        {
            "util.py": "def helper():\n    return 1\n",
            "main.py": "import importlib\nu = importlib.import_module('util')\n",
        },
    )
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


def test_07c_competing_intents_stay_unbound():
    db = Database(":memory:")
    try:
        bindings = ActionBindings(db)
        for step in ("one", "two"):
            bindings.record_intent("t", step, "write", run_id="r", now=1)
        result = bindings.heal_pending(
            "t",
            [
                {
                    "effect_id": "e",
                    "task_id": "t",
                    "tool_id": "write",
                    "run_id": "r",
                    "created_at": 2,
                }
            ],
        )
        assert not result["healed"] and len(result["ambiguous"]) == 2
        assert bindings.get("e") is None
    finally:
        db.close()


def test_07c_resolution_failure_rolls_back_both_records():
    db = Database(":memory:")
    try:
        bindings = ActionBindings(db)
        intent = bindings.record_intent("t", "s", "write", now=1)
        db._conn.execute(
            "CREATE TRIGGER reject_review_fill BEFORE UPDATE ON action_binding_intents BEGIN SELECT RAISE(ABORT, 'review fault'); END"
        )
        db._conn.commit()
        with pytest.raises(Exception, match="review fault"):
            bindings.resolve_intent(intent, "e")
        assert (
            bindings.get("e") is None and bindings.intent(intent)["effect_id"] is None
        )
    finally:
        db.close()
