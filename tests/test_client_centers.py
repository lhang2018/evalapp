"""用户端中心页面数据接口（PRD 89.3 / 89.4）。

覆盖：记忆查看与过滤、纠正、删除、权限范围；
工作流定义/版本/历史运行与产物。
"""

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.memory.store import Credibility, MemoryItem, MemoryKind, Scope


class TestClientCenters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_centers_")
        self.app = App(data_dir=self.tmp)
        self.server = ApiServer(self.app, port=0)
        self.port = self.server.httpd.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _memory(self, content, **kwargs):
        item = MemoryItem(content=content, **kwargs)
        self.app.memory.write(item)
        return item

    def test_memory_list_filters_and_invalid_query(self):
        pref = self._memory("偏好深色主题", kind=MemoryKind.USER_PREFERENCE,
                            scope=Scope.SHARED, status=Credibility.VERIFIED,
                            evidence={"source": "user"})
        self._memory("项目使用 Python", kind=MemoryKind.PROJECT_STATE,
                     scope=Scope.PROJECT, status=Credibility.CANDIDATE)
        revoked = self._memory("旧偏好", kind=MemoryKind.USER_PREFERENCE,
                               scope=Scope.SHARED)
        self.app.memory.set_status(revoked.memory_id, Credibility.REVOKED)

        status, out = self._request("/memory")
        self.assertEqual(status, 200)
        ids = [i["memory_id"] for i in out["items"]]
        self.assertIn(pref.memory_id, ids)
        self.assertNotIn(revoked.memory_id, ids)
        self.assertNotIn("evidence", out["items"][0])

        status, out = self._request("/memory?kind=user_preference")
        self.assertEqual(status, 200)
        self.assertEqual([i["memory_id"] for i in out["items"]],
                         [pref.memory_id])

        status, out = self._request("/memory?status=revoked")
        self.assertEqual(status, 200)
        self.assertEqual([i["memory_id"] for i in out["items"]],
                         [revoked.memory_id])

        status, _ = self._request("/memory?kind=nope")
        self.assertEqual(status, 400)
        status, _ = self._request("/memory?limit=abc")
        self.assertEqual(status, 400)

    def test_memory_correct_and_delete_are_audited(self):
        item = self._memory("偏好浅色主题", kind=MemoryKind.USER_PREFERENCE,
                            scope=Scope.SHARED,
                            status=Credibility.CANDIDATE)

        status, out = self._request(
            f"/memory/{item.memory_id}/correct", "POST",
            {"content": "偏好深色主题", "reason": "用户纠正"})
        self.assertEqual(status, 200)
        self.assertEqual(out["item"]["content"], "偏好深色主题")
        self.assertEqual(out["item"]["source"], "user")
        self.assertEqual(out["item"]["status"], "verified")
        stored = self.app.memory.get(item.memory_id)
        self.assertEqual(stored.content, "偏好深色主题")
        self.assertEqual(stored.evidence["user_correction"]["reason"], "用户纠正")

        status, _ = self._request(
            f"/memory/{item.memory_id}/correct", "POST", {"content": " "})
        self.assertEqual(status, 400)

        status, out = self._request(f"/memory/{item.memory_id}", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertIsNone(self.app.memory.get(item.memory_id))
        status, _ = self._request(f"/memory/{item.memory_id}", "DELETE")
        self.assertEqual(status, 404)

        actions = [a["action"] for a in self.app.audit.list(limit=20)]
        self.assertIn("memory.correct", actions)
        self.assertIn("memory.delete", actions)

    def test_memory_permission_scope_blocks_mutation(self):
        item = self._memory("受限记忆", kind=MemoryKind.RUNTIME_EXPERIENCE,
                            scope=Scope.TASK, allowed=False)
        status, out = self._request("/memory")
        self.assertNotIn(item.memory_id, [i["memory_id"] for i in out["items"]])
        status, _ = self._request(
            f"/memory/{item.memory_id}/correct", "POST", {"content": "改"})
        self.assertEqual(status, 403)
        status, _ = self._request(f"/memory/{item.memory_id}", "DELETE")
        self.assertEqual(status, 403)
        self.assertIsNotNone(self.app.memory.get(item.memory_id))

    def test_workflow_detail_versions_runs_and_artifacts(self):
        self.app.db.save_workflow_def(
            "wf_comic", "1.0.0", "published",
            {"workflow_id": "wf_comic", "version": "1.0.0", "nodes": []})
        self.app.db.save_workflow_def(
            "wf_comic", "1.1.0", "candidate",
            {"workflow_id": "wf_comic", "version": "1.1.0", "nodes": []})
        task = Task(user_goal="生成漫剧", workflow_ref="wf_comic",
                    task_status=TaskStatus.SUCCEEDED, result="成片")
        self.app.db.save_task(task)
        self.app.db.save_checkpoint(
            "cp_client_centers", task.task_id, time.time(), {
                "workflow_version": "1.0.0",
                "completed_nodes": ["script", "compose"],
                "pending_nodes": [],
                "task_state": {"workflow_outputs": {
                    "script": "脚本", "compose": "artifact://final.mp4"}},
            })

        status, out = self._request("/workflows/wf_comic")
        self.assertEqual(status, 200)
        self.assertEqual({v["version"] for v in out["versions"]},
                         {"1.0.0", "1.1.0"})
        self.assertEqual(len(out["runs"]), 1)
        run = out["runs"][0]
        self.assertEqual(run["task_id"], task.task_id)
        self.assertEqual(run["status"], "SUCCEEDED")
        self.assertEqual(run["workflow_version"], "1.0.0")
        self.assertEqual(run["artifacts"]["compose"],
                         "artifact://final.mp4")
        self.assertIn("script", run["completed_nodes"])

        status, out = self._request("/workflows/wf_comic?runs=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(out["runs"]), 1)

        status, _ = self._request("/workflows/missing")
        self.assertEqual(status, 404)

    def test_workflow_list_remains_compatible(self):
        self.app.db.save_workflow_def(
            "wf_a", "1.0.0", "published",
            {"workflow_id": "wf_a", "version": "1.0.0", "nodes": []})
        status, out = self._request("/workflows")
        self.assertEqual(status, 200)
        self.assertEqual(out["workflows"][0]["workflow_id"], "wf_a")


if __name__ == "__main__":
    unittest.main()
