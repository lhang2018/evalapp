"""Follow-up on the explicitly assigned 18-L and 24-F review boundaries."""
import sqlite3
import typing

import pytest

from evoagent.memory.scope_policy import ScopePolicyStore
from evoagent.tools.refactor import analyze_rename


@pytest.mark.parametrize("parameter", [
    '*items: "Helper"', '**items: "Helper"', '*items: "list[Helper]"',
])
def test_forward_annotations_on_variadic_parameters_need_clarification(tmp_path, parameter):
    source = f'class Helper:\n    pass\n\ndef collect({parameter}):\n    pass\n'
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    # The original annotations resolve; accepting a partial rename would break
    # typing.get_type_hints despite both source versions still compiling.
    namespace = {}
    exec(compile(source, "sample.py", "exec"), namespace)
    assert typing.get_type_hints(namespace["collect"])
    result = analyze_rename(tmp_path, "Helper", "Renamed")
    if result.status == "ok":
        changed = result.edits[0]["new_text"]
        namespace = {}
        exec(compile(changed, "sample.py", "exec"), namespace)
        with pytest.raises(NameError):
            typing.get_type_hints(namespace["collect"])
    assert result.status == "needs_clarification"
    assert result.edits == ()


def test_non_string_variadic_annotations_can_be_renamed(tmp_path):
    source = 'class Helper:\n    pass\n\ndef collect(*args: Helper, **kwargs: Helper):\n    pass\n'
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    result = analyze_rename(tmp_path, "Helper", "Renamed")
    assert result.status == "ok"
    namespace = {}
    exec(compile(result.edits[0]["new_text"], "sample.py", "exec"), namespace)
    assert set(typing.get_type_hints(namespace["collect"]).values()) == {namespace["Renamed"]}


@pytest.mark.parametrize("already_initialized", [False, True])
@pytest.mark.parametrize("commit", [False, True])
def test_scope_store_construction_never_commits_the_host_transaction(tmp_path, already_initialized, commit):
    path = str(tmp_path / "scope.db")
    conn = sqlite3.connect(path)
    observer = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    observer.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE host_pending(value TEXT)")
        if already_initialized:
            ScopePolicyStore(conn)
        conn.execute("INSERT INTO host_pending VALUES ('not decided')")
        store = ScopePolicyStore(conn)
        assert conn.in_transaction
        assert observer.execute("SELECT * FROM host_pending").fetchall() == []
        store.set_principal_policy(authenticated="review", multi_principal=True,
                                   allow=["user"], deny=[], actor="review", reason="review")
        assert conn.in_transaction
        assert observer.execute("SELECT * FROM host_pending").fetchall() == []
        conn.commit() if commit else conn.rollback()
        assert len(observer.execute("SELECT * FROM host_pending").fetchall()) == int(commit)
        if commit:
            assert ScopePolicyStore(observer).principal_policy("review")["allow"] == ["user"]
    finally:
        conn.close()
        observer.close()


class FailingSchemaConnection(sqlite3.Connection):
    fail_schema = False

    def execute(self, sql, parameters=()):
        if self.fail_schema and "CREATE TABLE IF NOT EXISTS memory_scope_policy_migrations" in sql:
            raise sqlite3.OperationalError("schema initialization interrupted")
        return super().execute(sql, parameters)


def test_scope_store_rolls_back_its_own_partial_schema_on_initialization_error(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "fresh.db"), factory=FailingSchemaConnection)
    conn.row_factory = sqlite3.Row
    try:
        conn.fail_schema = True
        with pytest.raises(sqlite3.OperationalError, match="initialization interrupted"):
            ScopePolicyStore(conn)
        assert not conn.in_transaction
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert tables == []
        conn.fail_schema = False
        ScopePolicyStore(conn)
        assert not conn.in_transaction
        assert len(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()) == 3
    finally:
        conn.close()
