"""Shared asset storage must never commit host work or leave half-written assets."""
import sqlite3

import pytest

from evoagent.assets.composition import compose
from evoagent.assets.entity import DependencyError
from evoagent.assets.repository import AssetRepository


def connect(path, **kwargs):
    conn = sqlite3.connect(str(path), **kwargs)
    conn.row_factory = sqlite3.Row
    return conn


def register(repo, name="sample", dependencies=()):
    return repo.register(asset_id=name, asset_type="skill", namespace="project", version="1",
                         lifecycle_state="candidate", content={"required_inputs": [], "produced_outputs": []},
                         source_refs=("test:source",), dependency_refs=dependencies)


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "assets.db")
    observer = connect(tmp_path / "assets.db")
    try:
        repo = AssetRepository(conn)
        conn.execute("CREATE TABLE host_pending(value TEXT)")
        conn.commit()
        yield conn, repo, observer
    finally:
        conn.close()
        observer.close()


def snapshot(conn):
    return (list(map(tuple, conn.execute("SELECT asset_id,lifecycle_state FROM assets ORDER BY asset_id"))),
            list(map(tuple, conn.execute("SELECT asset_id,action FROM asset_events ORDER BY event_id"))))


@pytest.mark.parametrize("initialized", [False, True])
@pytest.mark.parametrize("commit", [False, True])
def test_constructor_preserves_ambient_transaction(tmp_path, initialized, commit):
    conn = connect(tmp_path / "construct.db")
    observer = connect(tmp_path / "construct.db")
    try:
        conn.execute("CREATE TABLE host_pending(value TEXT)")
        if initialized:
            AssetRepository(conn)
        conn.execute("INSERT INTO host_pending VALUES ('host')")
        AssetRepository(conn)
        assert conn.in_transaction
        assert observer.execute("SELECT * FROM host_pending").fetchall() == []
        conn.commit() if commit else conn.rollback()
        assert len(observer.execute("SELECT * FROM host_pending").fetchall()) == int(commit)
    finally:
        conn.close()
        observer.close()


def operation(repo, kind):
    if kind == "register":
        return lambda: register(repo)
    item = register(repo, dependencies=("missing@1",) if kind == "dependency_failure" else ())
    if kind == "idempotent":
        return lambda: register(repo)
    if kind == "transition":
        return lambda: repo.transition("sample", "1", "validating")
    if kind == "approved":
        repo.transition("sample", "1", "validating")
        return lambda: repo.transition("sample", "1", "approved", evidence_refs=("test:evaluation",))
    if kind in ("dependency_success", "dependency_failure"):
        def check():
            if kind == "dependency_failure":
                with pytest.raises(DependencyError):
                    repo.check_dependencies(item)
            else:
                repo.check_dependencies(item)
        return check
    assert kind == "collect"
    repo.transition("sample", "1", "retired")
    return lambda: repo.collect("sample", "1")


@pytest.mark.parametrize("kind", ["register", "idempotent", "transition", "approved",
                                  "dependency_success", "dependency_failure", "collect"])
@pytest.mark.parametrize("commit", [False, True])
def test_all_write_entries_leave_commit_decision_to_host(store, kind, commit):
    conn, repo, observer = store
    act = operation(repo, kind)
    before = snapshot(observer)
    conn.execute("INSERT INTO host_pending VALUES ('host')")
    act()
    assert conn.in_transaction
    assert observer.execute("SELECT * FROM host_pending").fetchall() == []
    assert snapshot(observer) == before
    after = snapshot(conn)
    conn.commit() if commit else conn.rollback()
    assert snapshot(observer) == (after if commit else before)
    assert len(observer.execute("SELECT * FROM host_pending").fetchall()) == int(commit)


@pytest.mark.parametrize("kind,event", [("register", "register"), ("transition", "transition"),
                                       ("approved", "transition"), ("collect", "collect")])
@pytest.mark.parametrize("ambient", [False, True])
def test_event_failure_rolls_back_only_asset_operation(store, kind, event, ambient):
    conn, repo, observer = store
    act = operation(repo, kind)
    before = snapshot(observer)
    conn.execute(f"CREATE TRIGGER refuse_asset_event BEFORE INSERT ON asset_events "
                 f"WHEN NEW.action='{event}' BEGIN SELECT RAISE(ABORT, 'event storage failed'); END")
    conn.commit()
    if ambient:
        conn.execute("INSERT INTO host_pending VALUES ('host')")
    with pytest.raises(sqlite3.IntegrityError, match="event storage failed"):
        act()
    assert conn.in_transaction is ambient
    assert snapshot(conn) == before
    if ambient:
        assert conn.execute("SELECT value FROM host_pending").fetchone()[0] == "host"
        assert observer.execute("SELECT * FROM host_pending").fetchall() == []
        conn.commit()
    else:
        # A subsequent unrelated commit must not make a failed asset write real.
        conn.execute("INSERT INTO host_pending VALUES ('next request')")
        conn.commit()
    assert snapshot(observer) == before


class FailingSchemaConnection(sqlite3.Connection):
    fail = True

    def execute(self, sql, parameters=()):
        if self.fail and "CREATE TABLE IF NOT EXISTS asset_events" in sql:
            raise sqlite3.OperationalError("asset schema failed")
        return super().execute(sql, parameters)


def test_owned_schema_failure_leaves_no_partial_tables_and_can_retry(tmp_path):
    conn = connect(tmp_path / "schema.db", factory=FailingSchemaConnection)
    try:
        with pytest.raises(sqlite3.OperationalError, match="asset schema failed"):
            AssetRepository(conn)
        assert not conn.in_transaction
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        conn.fail = False
        register(AssetRepository(conn))
        assert not conn.in_transaction
    finally:
        conn.close()


def test_nested_schema_failure_preserves_host_work_without_partial_asset_tables(tmp_path):
    conn = connect(tmp_path / "nested-schema.db", factory=FailingSchemaConnection)
    try:
        conn.execute("CREATE TABLE host_pending(value TEXT)")
        conn.execute("INSERT INTO host_pending VALUES ('host')")
        with pytest.raises(sqlite3.OperationalError, match="asset schema failed"):
            AssetRepository(conn)
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM host_pending").fetchone()[0] == "host"
        assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["host_pending"]
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("ambient", [False, True])
def test_interrupt_after_event_write_does_not_escape_transaction_cleanup(store, monkeypatch, ambient):
    conn, repo, observer = store
    before = snapshot(observer)
    if ambient:
        conn.execute("INSERT INTO host_pending VALUES ('host')")
    original = repo._event
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("synthetic interruption")
    monkeypatch.setattr(repo, "_event", interrupted)
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        register(repo)
    assert conn.in_transaction is ambient
    assert snapshot(conn) == before
    if ambient:
        assert conn.execute("SELECT value FROM host_pending").fetchone()[0] == "host"
        conn.commit()
    assert snapshot(observer) == before


def test_composition_draft_is_rolled_back_with_host_transaction(store):
    conn, repo, observer = store
    register(repo)
    repo.transition("sample", "1", "validating")
    repo.transition("sample", "1", "approved", evidence_refs=("test:evaluation",))
    repo.transition("sample", "1", "active")
    before = snapshot(observer)
    conn.execute("INSERT INTO host_pending VALUES ('host')")
    result = compose(repo, ["sample@1"], new_asset_id="composed", new_version="1")
    assert result.asset_ref == "composed@1"
    assert conn.in_transaction
    conn.rollback()
    assert snapshot(observer) == before
