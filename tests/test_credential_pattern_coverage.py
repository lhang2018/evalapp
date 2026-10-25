"""29-C 复核发现修复：凭据模式覆盖 sk-live-/stripe 形态（残留风险回归）。

复核发现：`sk-live-1234567890abcdef` 因 `sk-` 后含连字符不匹配旧模式而原样入库。
修复后：允许 `sk-` 后含 `-`/`_`；新增 stripe `sk_live_/rk_test_` 形态；并验证
不误伤普通文本（disk-usage / sky-is-blue / task-1）。
"""

from evoagent.observability.credentials import contains_secret, scrub_text
from evoagent.observability.feedback import FeedbackError, FeedbackStore


def test_sk_with_hyphen_now_detected_and_redacted():
    text = "我的密钥是 sk-live-1234567890abcdef 请查"
    assert contains_secret(text) is True
    cleaned = scrub_text(text)
    assert "1234567890abcdef" not in cleaned
    assert "redacted" in cleaned


def test_stripe_style_keys_detected():
    assert contains_secret("sk_live_abc123def456") is True
    assert contains_secret("rk_test_abc123def456") is True


def test_existing_forms_still_detected():
    assert contains_secret("sk-abcdefgh1234") is True
    assert contains_secret("Bearer abcdefgh12345678") is True
    assert contains_secret("api_key=abcdef123456") is True


def test_normal_text_not_false_positive():
    for text in ("disk-usage is high", "sky-is-blue", "task-1 finished",
                 "sk-short"):   # 少于阈值不误判
        assert contains_secret(text) is False, text


def test_feedback_store_rejects_previously_bypassing_secret():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE user_feedback(feedback_id TEXT PRIMARY KEY,"
        " agg_key TEXT UNIQUE, category TEXT, text TEXT, task_id TEXT,"
        " run_id TEXT, manifest_id TEXT, evidence_refs TEXT,"
        " cause_status TEXT, count INTEGER, first_seen_at REAL,"
        " last_seen_at REAL)")
    store = FeedbackStore(conn)
    try:
        store.submit(category="bug", text="我的密钥 sk-live-1234567890abcdef")
        raise AssertionError("应拒绝包含凭据的反馈")
    except FeedbackError as exc:
        assert "凭据" in str(exc) or "秘密" in str(exc)
    rows = conn.execute("SELECT COUNT(*) FROM user_feedback").fetchone()[0]
    assert rows == 0, "被拒绝的凭据文本不得入库"
    conn.close()
