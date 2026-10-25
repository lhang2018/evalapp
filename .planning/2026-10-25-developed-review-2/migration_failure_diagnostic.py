import json
import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        app = getattr(getattr(item, "instance", None), "app", None)
        manager = getattr(app, "migrations", None)
        if manager is not None:
            for migration in manager.list():
                print("MIGRATION_FAILURE_DIAGNOSTIC=" + json.dumps({"id": migration.migration_id, "status": migration.status, "reasons": migration.blocked_reasons, "regression": migration.regression}, ensure_ascii=False))
