"""候选代码隔离执行测试（PRD 47.4 / Arch 18.3）。"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.evolution.sandbox import SandboxRunner


class TestSandboxRunner(unittest.TestCase):
    def setUp(self):
        self.runner = SandboxRunner()
        self.outside = tempfile.mkdtemp(prefix="evoagent_outside_")

    def tearDown(self):
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_basic_code_passes(self):
        out = self.runner.run_code("print('hello', 1 + 1)")
        self.assertEqual(out.status, "passed")
        self.assertEqual(out.exit_code, 0)
        self.assertIn("hello 2", out.stdout)
        self.assertEqual(out.violations, [])

    def test_write_inside_sandbox_allowed(self):
        out = self.runner.run_code(
            "open('a.txt', 'w').write('x')\nprint(open('a.txt').read())")
        self.assertEqual(out.status, "passed")
        self.assertIn("x", out.stdout)

    def test_write_outside_sandbox_blocked(self):
        target = os.path.join(self.outside, "escape.txt")
        out = self.runner.run_code(
            f"open(r'{target}', 'w').write('x')\nprint('done')")
        self.assertEqual(out.status, "violation")
        self.assertTrue(any("write outside sandbox" in v
                            for v in out.violations))
        self.assertFalse(os.path.exists(target))

    def test_read_outside_sandbox_blocked(self):
        target = os.path.join(self.outside, "secret.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("top-secret")
        out = self.runner.run_code(f"print(open(r'{target}').read())")
        self.assertEqual(out.status, "violation")
        self.assertTrue(any("read outside sandbox" in v
                            for v in out.violations))
        self.assertNotIn("top-secret", out.stdout)

    def test_network_blocked(self):
        out = self.runner.run_code(
            "import socket\nsocket.getaddrinfo('localhost', 80)")
        self.assertEqual(out.status, "violation")
        self.assertTrue(any("socket" in v for v in out.violations))

    def test_subprocess_blocked(self):
        out = self.runner.run_code(
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', 'print(1)'])")
        self.assertEqual(out.status, "violation")
        self.assertTrue(any("subprocess" in v for v in out.violations))

    def test_credentials_not_inherited(self):
        os.environ["EVOAGENT_API_KEY"] = "should-not-leak"
        os.environ["MY_SECRET_TOKEN"] = "nope"
        try:
            out = self.runner.run_code(
                "import os\n"
                "print(os.environ.get('EVOAGENT_API_KEY'), "
                "os.environ.get('MY_SECRET_TOKEN'))")
        finally:
            os.environ.pop("EVOAGENT_API_KEY", None)
            os.environ.pop("MY_SECRET_TOKEN", None)
        self.assertEqual(out.status, "passed")
        self.assertIn("None None", out.stdout)
        self.assertNotIn("should-not-leak", out.stdout + out.stderr)

    def test_timeout_is_enforced(self):
        out = self.runner.run_code("import time; time.sleep(3)", timeout=0.5)
        self.assertEqual(out.status, "timeout")
        self.assertLess(out.duration_s, 2.5)

    def test_nonzero_exit_is_failed(self):
        out = self.runner.run_code("raise ValueError('boom')")
        self.assertEqual(out.status, "failed")
        self.assertNotEqual(out.exit_code, 0)
        self.assertIn("boom", out.stderr)

    def test_output_is_truncated(self):
        out = self.runner.run_code("print('x' * 5000)", output_limit=300)
        self.assertEqual(out.status, "passed")
        self.assertLess(len(out.stdout), 1000)
        self.assertIn("已截断", out.stdout)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.runner.run_file(os.path.join(self.outside, "nope.py"))


class TestSandboxApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="evoagent_sandbox_api_")
        cls.app = App(data_dir=cls.tmp)
        cls.app.config.api_token = "op"
        cls.app.config.api_admin_token = "root"
        cls.srv = ApiServer(cls.app, host="127.0.0.1", port=18303)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:18303"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, payload, token="root"):
        req = urllib.request.Request(
            self.base + "/evolution/sandbox/run",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_admin_can_run_code(self):
        st, out = self._post({"code": "print('api-ok')"})
        self.assertEqual(st, 200)
        self.assertEqual(out["status"], "passed")
        self.assertIn("api-ok", out["stdout"])

    def test_operator_forbidden(self):
        st, out = self._post({"code": "print(1)"}, token="op")
        self.assertEqual(st, 403)

    def test_missing_code_400(self):
        st, out = self._post({})
        self.assertEqual(st, 400)

    def test_missing_file_404(self):
        st, out = self._post({"path": "does-not-exist-candidate.py"})
        self.assertEqual(st, 404)

    def test_violation_reported(self):
        st, out = self._post({"code": "import socket\n"
                                      "socket.getaddrinfo('localhost', 80)"})
        self.assertEqual(st, 200)
        self.assertEqual(out["status"], "violation")
        self.assertTrue(out["violations"])


if __name__ == "__main__":
    unittest.main()
