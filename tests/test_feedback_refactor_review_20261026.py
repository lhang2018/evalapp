"""Synthetic private-material and static module-identity regression probes."""
import pytest
import subprocess
import sys

from evoagent.core.persistence import Database
from evoagent.observability.credentials import (
    CredentialLeakError, assert_context_safe, contains_secret, scrub_text,
)
from evoagent.observability.feedback import FeedbackError, FeedbackStore
from evoagent.tools.refactor import analyze_rename


def private_fixture(kind, closed):
    # Deliberately invalid key bytes; never use real credentials in regression data.
    label = (kind + " " if kind else "") + "PRIVATE KEY"
    text = "-----BEGIN " + label + "-----\nsynthetic fixture bytes\n"
    return text + ("-----END " + label + "-----" if closed else "")


@pytest.mark.parametrize("kind", ["", "RSA", "EC", "DSA", "OPENSSH", "ENCRYPTED"])
@pytest.mark.parametrize("closed", [True, False])
def test_private_envelope_and_truncated_material_never_enter_feedback(kind, closed):
    text = private_fixture(kind, closed)
    assert contains_secret(text)
    cleaned = scrub_text(text)
    assert "synthetic fixture bytes" not in cleaned
    assert not contains_secret(cleaned)
    with pytest.raises(CredentialLeakError):
        assert_context_safe({"diagnostic": text})
    db = Database(":memory:")
    try:
        with pytest.raises(FeedbackError):
            FeedbackStore(db._conn).submit(category="bug", text=text)
        assert db._conn.execute("SELECT COUNT(*) FROM user_feedback").fetchone()[0] == 0
    finally:
        db.close()


def test_redacting_closed_private_envelope_retains_surrounding_notes():
    text = "before note\n" + private_fixture("RSA", True) + "\nafter note"
    cleaned = scrub_text(text)
    assert "before note" in cleaned and "after note" in cleaned
    assert "synthetic fixture bytes" not in cleaned


def test_public_certificate_marker_is_not_a_private_key():
    text = "-----BEGIN CERTIFICATE-----\npublic fixture\n-----END CERTIFICATE-----"
    assert not contains_secret(text)
    assert scrub_text(text) == text


def project(root, files):
    for name, source in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")


def run_consumer(root, expression="consumer.answer"):
    return subprocess.check_output([sys.executable, "-X", "utf8", "-c",
        "import consumer; print(" + expression + ")"], cwd=root, text=True, timeout=5).strip()


def test_rebound_from_import_alias_cannot_rename_foreign_object_attribute(tmp_path):
    project(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/target.py": "def helper():\n    return 'target'\n",
        "external_lib.py": "from types import SimpleNamespace\nobj = SimpleNamespace(helper=lambda: 'foreign')\n",
        "consumer.py": "from external_lib import obj as mod\nfirst = mod.helper()\nfrom pkg import target as mod\nsecond = mod.helper()\n",
    })
    assert run_consumer(tmp_path, "consumer.first + ' ' + consumer.second") == "foreign target"
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


@pytest.mark.parametrize("root_package", [False, True])
def test_package_export_shadow_cannot_be_mistaken_for_module(tmp_path, root_package):
    prefix = "" if root_package else "pkg/"
    project(tmp_path, {
        prefix + "__init__.py": "from types import SimpleNamespace\ntarget = SimpleNamespace(helper=lambda: 'foreign')\n",
        prefix + "target.py": "def helper():\n    return 'target'\n",
        "consumer.py": ("from . import target as mod\n" if root_package else "from pkg import target as mod\n") + "answer = mod.helper()\n",
    })
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


def test_normal_from_package_module_import_remains_renamable(tmp_path):
    project(tmp_path, {"pkg/__init__.py": "", "pkg/target.py": "def helper():\n    return 1\n",
        "consumer.py": "from pkg import target as mod\nanswer = mod.helper()\n"})
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "ok"
    assert "mod.compute()" in next(e["new_text"] for e in result.edits if e["path"] == "consumer.py")
    for edit in result.edits:
        (tmp_path / edit["path"]).write_text(edit["new_text"], encoding="utf-8")
    assert run_consumer(tmp_path) == "1"


@pytest.mark.parametrize("initializer", ["from . import target\n", "import pkg.target as target\n"])
def test_explicit_package_module_export_is_not_confused_with_foreign_export(tmp_path, initializer):
    project(tmp_path, {"pkg/__init__.py": initializer, "pkg/target.py": "def helper():\n    return 1\n",
        "consumer.py": "from pkg import target as mod\nanswer = mod.helper()\n"})
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "ok"
    assert "mod.compute()" in next(e["new_text"] for e in result.edits if e["path"] == "consumer.py")
    for edit in result.edits:
        (tmp_path / edit["path"]).write_text(edit["new_text"], encoding="utf-8")
    assert run_consumer(tmp_path) == "1"


def test_root_package_relative_module_export_remains_renamable(tmp_path):
    project(tmp_path, {"__init__.py": "from . import target\n", "target.py": "def helper():\n    return 1\n",
        "consumer.py": "from . import target as mod\nanswer = mod.helper()\n"})
    assert analyze_rename(tmp_path, "helper", "compute").status == "ok"


def test_dynamic_package_export_does_not_prove_module_identity(tmp_path):
    project(tmp_path, {"pkg/__init__.py": "from types import SimpleNamespace\ndef __getattr__(name):\n    return SimpleNamespace(helper=lambda: 'foreign')\n",
        "pkg/target.py": "def helper():\n    return 1\n",
        "consumer.py": "from pkg import target as mod\nanswer = mod.helper()\n"})
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


@pytest.mark.parametrize("assignment", [
    "sys.modules[__name__].target = SimpleNamespace(helper=lambda: 'foreign')",
    "sys.modules[__name__].__dict__['target'] = SimpleNamespace(helper=lambda: 'foreign')",
])
def test_package_attribute_mutation_does_not_prove_module_identity(tmp_path, assignment):
    project(tmp_path, {"pkg/__init__.py": "import sys\nfrom types import SimpleNamespace\n" + assignment + "\n",
        "pkg/target.py": "def helper():\n    return 1\n",
        "consumer.py": "from pkg import target as mod\nanswer = mod.helper()\n"})
    assert run_consumer(tmp_path) == "foreign"
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits


@pytest.mark.parametrize("assignment", [
    "pkg.target = SimpleNamespace(helper=lambda: 'foreign')",
    "pkg.__dict__['target'] = SimpleNamespace(helper=lambda: 'foreign')",
    "pkg.target.__dict__['helper'] = lambda: 'foreign'",
])
def test_importer_cannot_rebind_a_package_prefix_and_keep_module_proof(tmp_path, assignment):
    project(tmp_path, {"pkg/__init__.py": "", "pkg/target.py": "def helper():\n    return 1\n",
        "consumer.py": "import pkg.target\nfrom types import SimpleNamespace\n" + assignment + "\nanswer = pkg.target.helper()\n"})
    assert run_consumer(tmp_path) == "foreign"
    result = analyze_rename(tmp_path, "helper", "compute")
    assert result.status == "needs_clarification" and not result.edits
