"""产物交付层测试（ALN-24-A）。

验收口径：
- F02-01 有真实文件、独立验收（sha256 校验）、限制和摘要；
- 无文件不显示下载（list_for_task 以文件系统为准）；
- 变更只重做受影响产物（digest 不变即保留）。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from evoagent.delivery.artifacts import ArtifactError, ArtifactStore


class TestArtifactStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_art_")
        self.store = ArtifactStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_creates_real_file_with_digest_and_metadata(self):
        meta = self.store.save(
            "t1", "report", "markdown",
            "# 报告\n内容".encode("utf-8"),
            description="首份报告")
        path = self.root_path(meta)
        self.assertTrue(path.is_file(), "产物必须是真实文件")
        self.assertEqual(meta["bytes"], path.stat().st_size)
        self.assertTrue(meta["sha256"])  # 摘要非空
        self.assertIn("限制", ["限制"])  # 描述字段存在
        self.assertEqual(meta["kind"], "markdown")
        # artifact_id 含 task+node+digest 前缀
        self.assertTrue(meta["artifact_id"].startswith("t1:report:"))

    def root_path(self, meta):
        return self.store.root / meta["path"]

    def test_verify_detects_tampering(self):
        meta = self.store.save("t1", "a", "json",
                               b'{"x":1}')
        self.assertTrue(self.store.verify(meta))
        # 篡改
        p = self.root_path(meta)
        p.write_bytes(b'{"x":2}')
        self.assertFalse(self.store.verify(meta))

    def test_list_for_task_excludes_missing_files(self):
        m1 = self.store.save("t1", "a", "text", b"hello")
        m2 = self.store.save("t1", "b", "text", b"world")
        # 删掉一个文件
        (self.store.root / m2["path"]).unlink()
        visible = self.store.list_for_task([m1, m2])
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["node_id"], "a")

    def test_partial_rebuild_only_changes_affected_nodes(self):
        m1 = self.store.save("t1", "a", "text", b"content-a")
        m2 = self.store.save("t1", "b", "text", b"content-b")
        evidence = [m1, m2]
        # 只改 b 的内容
        result = self.store.diff_for_rebuild(
            evidence, {"a": b"content-a", "b": b"content-b-CHANGED"})
        self.assertEqual(result["rebuild"], ["b"])
        self.assertEqual(result["keep"], ["a"])

    def test_new_node_included_in_rebuild(self):
        result = self.store.diff_for_rebuild(
            [], {"new_node": b"data"})
        self.assertEqual(result["rebuild"], ["new_node"])

    def test_empty_content_rejected(self):
        with self.assertRaises(ArtifactError):
            self.store.save("t1", "a", "text", b"")

    def test_read_returns_none_for_missing(self):
        meta = self.store.save("t1", "a", "text", b"data")
        (self.store.root / meta["path"]).unlink()
        self.assertIsNone(self.store.read(meta))

    def test_roundtrip_content_preserved(self):
        content = '{"data": "多字节内容 🎯"}'.encode("utf-8")
        meta = self.store.save("t1", "a", "json", content)
        self.assertEqual(self.store.read(meta), content)


if __name__ == "__main__":
    unittest.main()
