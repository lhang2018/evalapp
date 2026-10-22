"""Immutable regression snapshots, using the shared database connection and lock."""
import hashlib
import json
import time
from dataclasses import asdict

from .evaluator import EvalCase


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


class RegressionStore:
    def __init__(self, db):
        self.db = db

    def freeze(self, case):
        payload = asdict(case)
        encoded, sha = encode(payload), digest(payload)
        with self.db._lock:
            row = self.db._conn.execute("SELECT payload,digest FROM regression_assets WHERE case_id=?",
                                        (case.case_id,)).fetchone()
            if row:
                if row["digest"] != sha or row["payload"] != encoded:
                    raise ValueError("回归资产不可覆盖: " + case.case_id)
                return
            self.db._conn.execute("INSERT INTO regression_assets VALUES (?,?,?,?)",
                                  (case.case_id, encoded, sha, time.time()))
            self.db._conn.commit()

    def all(self):
        with self.db._lock:
            rows = self.db._conn.execute("SELECT payload,digest FROM regression_assets ORDER BY case_id").fetchall()
        cases = []
        for row in rows:
            value = json.loads(row["payload"])
            if digest(value) != row["digest"]:
                raise ValueError("回归资产摘要不一致，禁止用于门禁")
            cases.append(EvalCase(**value))
        return cases
