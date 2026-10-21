"""sync 域建表语句（客户端幂等提交、双端协同与版本对账）。

对应 PRD 72-76 与补充设计 A §3/§5/§7。
"""

DOMAIN = "sync"

TABLES = [
    # 客户端幂等提交（PRD 74）：同一 client_request_id 只创建一个任务
    """CREATE TABLE IF NOT EXISTS client_requests(
        client_request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
        created_at REAL)""",
    # 双端协同（补充 A §3）：设备安装实例
    """CREATE TABLE IF NOT EXISTS device_installations(
        installation_id TEXT PRIMARY KEY, device_id TEXT, platform TEXT,
        app_version TEXT, current_release_id TEXT, target_release_id TEXT,
        status TEXT, created_at REAL, last_seen_at REAL, data TEXT)""",
    # 设备部署记录（补充 A §5）
    """CREATE TABLE IF NOT EXISTS device_deployments(
        deployment_id TEXT PRIMARY KEY, installation_id TEXT,
        release_id TEXT, action TEXT, outcome TEXT, created_at REAL,
        data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_device_deployments_installation
        ON device_deployments(installation_id, created_at)""",
    # 同步批次回执（补充 C §3）
    """CREATE TABLE IF NOT EXISTS sync_batches(
        batch_id TEXT PRIMARY KEY, receipt_id TEXT, client_id TEXT,
        installation_id TEXT, received_at REAL, event_count INTEGER,
        accepted INTEGER, duplicates INTEGER, rejected INTEGER,
        data TEXT)""",
    # 运行清单（补充 C §5 / ADR-012）
    """CREATE TABLE IF NOT EXISTS runtime_manifests(
        manifest_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT,
        device_id TEXT, installation_id TEXT, created_at REAL,
        data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_runtime_manifests_task
        ON runtime_manifests(task_id, created_at)""",
    # 候选阶段同步（补充 A §2/§8）
    """CREATE TABLE IF NOT EXISTS device_candidate_states(
        installation_id TEXT, candidate_id TEXT, status TEXT,
        release_id TEXT, first_seen_at REAL, updated_at REAL, data TEXT,
        PRIMARY KEY(installation_id, candidate_id))""",
]