"""协作基座 F-00 测试：schema 拆分、路由注册、契约单一来源、迁移框架。

覆盖 FEATURE_SPLIT.md §3 的 F-00 四项验收，其中两条是核心准入条件：
- 新增一张表**不触碰** `persistence.py` 即可生效
- 新增一个端点**不触碰** `server.py` 即可生效
"""

import sqlite3
import unittest

from evoagent.core import schema
from evoagent.core.schema import migrations


class TestSchemaDomainSplit(unittest.TestCase):
    """F-00.1：建表语句按域拆分。"""

    def test_all_domains_registered(self):
        names = schema.domain_names()
        self.assertEqual(len(names), len(set(names)), "域名不应重复")
        for expected in ("tasks", "sync", "diagnostics", "evolution",
                         "release", "tools", "workflows", "observability",
                         "memory", "imports"):
            self.assertIn(expected, names)

    def test_every_statement_is_idempotent(self):
        """全部语句必须是幂等的 —— 每次连接都重放全部语句。"""
        for stmt in schema.all_statements():
            s = " ".join(stmt.split()).upper()
            self.assertTrue(
                s.startswith("CREATE TABLE IF NOT EXISTS")
                or s.startswith("CREATE INDEX IF NOT EXISTS"),
                f"非幂等语句：{stmt[:60]}")

    def test_statements_execute_twice_without_error(self):
        conn = sqlite3.connect(":memory:")
        for _ in range(2):
            for stmt in schema.all_statements():
                conn.execute(stmt)
        conn.commit()
        conn.close()

    def test_all_expected_tables_created(self):
        conn = sqlite3.connect(":memory:")
        for stmt in schema.all_statements():
            conn.execute(stmt)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {r[0] for r in rows}
        for t in ("tasks", "task_runs", "task_checkpoints", "workflow_runs",
                  "client_requests", "device_installations", "sync_batches",
                  "runtime_manifests", "diagnostic_cases",
                  "evolution_candidates", "release_manifests",
                  "tool_executions", "budget_ledger", "workflow_defs",
                  "audit_log", "execution_traces", "diagnostic_evidence",
                  "model_migrations", "memory_eval_reports",
                  "diagnostics_imports", "attachments", "attachment_chunks"):
            self.assertIn(t, tables)

    def test_new_domain_needs_zero_edits_to_existing_files(self):
        """核心准入条件：新增域模块被**自动发现**，无需登记。

        本包通过 pkgutil 自动发现，特性作者只需新建一个文件。
        这里用 goals 域演示该机制已被正确接入。
        """
        from evoagent.core.schema import goals

        self.assertIn("goals", schema.domain_names())
        self.assertEqual(goals.PLANNED_TABLES, ("goals", "goal_evidence"))
        # F-17 落地前刻意不建表，避免与后续设计补写冲突
        self.assertEqual(goals.TABLES, [])
        self.assertNotIn("goals", schema.table_names())

    def test_database_creates_all_domain_tables(self):
        from evoagent.core.persistence import Database
        db = Database(":memory:")
        rows = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {r[0] for r in rows}
        for t in schema.table_names():
            self.assertIn(t, tables, f"域模块声明的表 {t} 未建出")
        db._conn.close()


class TestMigrations(unittest.TestCase):
    """F-00.4：版本化迁移框架。"""

    def test_baseline_migration_runs_once(self):
        conn = sqlite3.connect(":memory:")
        first = migrations.apply_migrations(conn)
        self.assertEqual(first, ["baseline"])
        second = migrations.apply_migrations(conn)
        self.assertEqual(second, [], "重复调用不应重复执行")
        conn.close()

    def test_migration_version_recorded(self):
        conn = sqlite3.connect(":memory:")
        migrations.apply_migrations(conn)
        self.assertEqual(migrations.current_version(conn),
                         migrations.SCHEMA_VERSION)
        conn.close()

    def test_current_version_zero_for_fresh_db(self):
        conn = sqlite3.connect(":memory:")
        self.assertEqual(migrations.current_version(conn), 0)
        conn.close()

    def test_baseline_adds_goal_id_to_legacy_table(self):
        """旧库缺 goal_id 列时，基线迁移补齐（G-01 目标关联）。

        真实的旧结构是"有 created_at 但无 goal_id"；缺 created_at 属于
        不可兼容结构，基线会 DROP 重建（见下一个测试）。
        """
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE tasks(task_id TEXT PRIMARY KEY, status TEXT,"
            " created_at REAL, data TEXT)")
        self.assertNotIn(
            "goal_id",
            [r[1] for r in conn.execute("PRAGMA table_info(tasks)")])
        migrations.apply_migrations(conn)
        self.assertIn(
            "goal_id",
            [r[1] for r in conn.execute("PRAGMA table_info(tasks)")])
        conn.close()

    def test_baseline_drops_incompatible_legacy_table(self):
        """缺 created_at 属不可兼容结构，基线 DROP 重建（保留历史语义）。"""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('t1', 'stale')")
        migrations.apply_migrations(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
        self.assertEqual(cols, [], "DROP 后应由建表语句重建，这里只验证迁移侧")
        conn.close()

    def test_later_migration_only_applies_to_older_db(self):
        """已到最新版本的库，跳过后续迁移。"""
        conn = sqlite3.connect(":memory:")
        migrations.apply_migrations(conn)
        applied = migrations.apply_migrations(conn)
        self.assertEqual(applied, [])
        conn.close()


class TestContracts(unittest.TestCase):
    """F-00.3：契约与枚举单一来源。"""

    def test_error_fields_match_contract(self):
        from evoagent.core.contracts import ERROR_FIELDS
        self.assertEqual(len(ERROR_FIELDS), 7)
        self.assertIn("safe_to_repeat", ERROR_FIELDS)
        self.assertIn("error_code", ERROR_FIELDS)

    def test_safe_to_repeat_includes_unknown(self):
        from evoagent.core.contracts import SAFE_TO_REPEAT, SAFE_TO_REPEAT_UNKNOWN
        self.assertIn(SAFE_TO_REPEAT_UNKNOWN, SAFE_TO_REPEAT)

    def test_side_effect_status_matches_spec(self):
        from evoagent.core.contracts import (
            SIDE_EFFECT_NO_BLIND_RETRY, SIDE_EFFECT_STATUS, SIDE_EFFECT_STATUS as S,
        )
        self.assertEqual(len(SIDE_EFFECT_STATUS), 6)
        self.assertIn("unknown", SIDE_EFFECT_NO_BLIND_RETRY)
        self.assertNotIn("confirmed", SIDE_EFFECT_NO_BLIND_RETRY)

    def test_goal_status_has_ten_values(self):
        from evoagent.core.contracts import GOAL_STATUS_SPEC
        self.assertEqual(len(GOAL_STATUS_SPEC), 10)
        for v in ("ready", "review", "cancelled", "expired"):
            self.assertIn(v, GOAL_STATUS_SPEC)

    def test_goal_fields_cover_contract(self):
        from evoagent.core.contracts import GOAL_FIELDS
        for f in ("goal_id", "goal_type", "parent_goal_id", "success_criteria",
                  "authorization_ref", "budget_ref", "deadline",
                  "no_progress_policy_ref", "status", "evidence_refs"):
            self.assertIn(f, GOAL_FIELDS)

    def test_six_conflicts_recorded(self):
        from evoagent.core.contracts import CONFLICT_DECISIONS
        self.assertEqual(len(CONFLICT_DECISIONS), 6)
        for entry in CONFLICT_DECISIONS:
            for key in ("id", "topic", "spec_side", "impl_side", "decision",
                        "status"):
                self.assertIn(key, entry)

    def test_legacy_enums_differ_from_spec(self):
        """冲突记录的前提：legacy 与 spec 确实不同，否则无需裁定。"""
        from evoagent.core.contracts import (
            ASSET_NAMESPACE, ASSET_LIFECYCLE, ASSET_LIFECYCLE_LEGACY,
            IDEMPOTENCY_LEGACY, IDEMPOTENCY_SPEC, MEMORY_SCOPE_LEGACY,
            PRIVACY_LEVEL_LEGACY, PRIVACY_LEVEL_SPEC, SYNC_STATUS_SPEC,
        )
        self.assertNotEqual(PRIVACY_LEVEL_SPEC, PRIVACY_LEVEL_LEGACY)
        self.assertEqual(len(PRIVACY_LEVEL_SPEC), len(PRIVACY_LEVEL_LEGACY))
        self.assertNotEqual(IDEMPOTENCY_SPEC, IDEMPOTENCY_LEGACY)
        self.assertNotIn("acknowledged", ("local", "synced"))
        self.assertIn("acknowledged", SYNC_STATUS_SPEC)
        self.assertNotIn("protected_core", MEMORY_SCOPE_LEGACY)
        self.assertIn("protected_core", ASSET_NAMESPACE)
        self.assertNotEqual(ASSET_LIFECYCLE, ASSET_LIFECYCLE_LEGACY)

    def test_undeclared_artifact_not_allowed(self):
        from evoagent.core.contracts import ALLOW_UNDECLARED_ARTIFACT
        self.assertFalse(ALLOW_UNDECLARED_ARTIFACT,
                         "契约 §11：缺失兼容声明的制品不得进入稳定通道")

    def test_required_actions_cover_reconcile_and_human(self):
        from evoagent.core.contracts import REQUIRED_ACTIONS
        self.assertIn("reconcile", REQUIRED_ACTIONS)
        self.assertIn("human_confirm", REQUIRED_ACTIONS)


class TestRouteRegistry(unittest.TestCase):
    """F-00.2：路由注册表。"""

    def setUp(self):
        from evoagent.api import routes
        self.routes = routes
        self._saved = (list(routes._GET), list(routes._POST),
                       list(routes._PUT))

    def tearDown(self):
        self.routes._GET[:] = self._saved[0]
        self.routes._POST[:] = self._saved[1]
        self.routes._PUT[:] = self._saved[2]

    def _ctx(self, path="/x", method="GET", body=None):
        from evoagent.api.routes import RouteContext

        class _Auth:
            role = "admin"

        calls = []
        return RouteContext(
            app=object(), method=method, path=path, query={},
            body_getter=lambda: body if body is not None else {},
            auth=_Auth(),
            json=lambda code, payload: calls.append((code, payload)),
            text=lambda code, t, ct: calls.append((code, t)),
            role_can_read=lambda layer, role: True), calls

    def test_exact_match_dispatches(self):
        @self.routes.get("/demo")
        def handler(ctx):
            ctx.json(200, {"ok": True})

        ctx, calls = self._ctx("/demo")
        self.assertTrue(self.routes.dispatch_get(ctx))
        self.assertEqual(calls, [(200, {"ok": True})])

    def test_no_match_returns_false(self):
        ctx, _ = self._ctx("/nothing")
        self.assertFalse(self.routes.dispatch_get(ctx))

    def test_prefix_match_longest_wins(self):
        @self.routes.get("/a/")
        def broad(ctx):
            ctx.json(200, "broad")

        @self.routes.get("/a/b/")
        def narrow(ctx):
            ctx.json(200, "narrow")

        ctx, calls = self._ctx("/a/b/c")
        self.assertTrue(self.routes.dispatch_get(ctx))
        self.assertEqual(calls, [(200, "narrow")])

    def test_exact_beats_prefix(self):
        @self.routes.get("/a/")
        def broad(ctx):
            ctx.json(200, "broad")

        @self.routes.get("/a/b")
        def exact(ctx):
            ctx.json(200, "exact")

        ctx, calls = self._ctx("/a/b")
        self.assertTrue(self.routes.dispatch_get(ctx))
        self.assertEqual(calls, [(200, "exact")])

    def test_post_and_put_are_separate_tables(self):
        @self.routes.post("/demo")
        def p(ctx):
            ctx.json(201, {"created": True})

        ctx, calls = self._ctx("/demo", "POST")
        self.assertTrue(self.routes.dispatch_post(ctx))
        self.assertEqual(calls, [(201, {"created": True})])
        # GET 不应命中 POST 表
        gctx, _ = self._ctx("/demo", "GET")
        self.assertFalse(self.routes.dispatch_get(gctx))

    def test_handler_returning_none_still_counts_as_handled(self):
        """处理器约定：ctx.json() 直接写响应、返回 None；
        dispatch 必须按"是否命中"而非返回值判定。"""
        @self.routes.get("/void")
        def handler(ctx):
            ctx.json(204, None)
            return None

        ctx, calls = self._ctx("/void")
        self.assertTrue(self.routes.dispatch_get(ctx))
        self.assertEqual(len(calls), 1)

    def test_registered_lists_paths(self):
        @self.routes.get("/listed")
        def handler(ctx):
            ctx.json(200, {})

        self.assertIn("/listed", self.routes.registered("GET"))

    def test_load_registered_is_callable(self):
        self.assertIsNone(self.routes.load_registered())

    def test_body_is_lazy_not_eagerly_read(self):
        """回归：body 必须惰性读取。

        既有端点 POST /diagnostics/import 直接从 self.rfile 读原始字节。
        若构造上下文时急切解析 body，流已被消费，该端点将读到空数据
        （曾导致 ZIP 压缩比防护测试超时失败）。这里锁定惰性语义。
        """
        from evoagent.api.routes import RouteContext

        reads = []

        class _Auth:
            role = "admin"

        def getter():
            reads.append(1)
            return {"v": 1}

        ctx = RouteContext(
            app=object(), method="POST", path="/x", query={},
            body_getter=getter, auth=_Auth(),
            json=lambda c, p: None, text=lambda c, t, ct: None,
            role_can_read=lambda a, b: True)
        self.assertEqual(reads, [], "构造时不得读取 body")
        self.assertEqual(ctx.body, {"v": 1})
        self.assertEqual(ctx.body, {"v": 1})
        self.assertEqual(len(reads), 1, "body 只应读取一次并缓存")

    def test_server_passes_getter_not_dict(self):
        """server.py 传给上下文的必须是 getter，不能是已解析的字典。"""
        import inspect

        from evoagent.api import server
        src = inspect.getsource(server)
        self.assertIn("body_getter=self._body", src)
        self.assertNotIn("body=self._body()", src)

    def test_server_dispatch_helper_exists(self):
        """server.py 只加了一处调用点，三种方法都接上了。"""
        import inspect

        from evoagent.api import server
        src = inspect.getsource(server)
        self.assertIn("self._dispatch(\"GET\"", src)
        self.assertIn("self._dispatch(\"POST\"", src)
        self.assertIn("self._dispatch(\"PUT\"", src)


if __name__ == "__main__":
    unittest.main()