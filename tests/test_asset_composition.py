"""可组合能力资产测试（ALN-27-B）。

验收口径：
- 依赖闭包与输入输出兼容（递归闭包/类型检查/缺失拒绝）；
- 重用有真实执行证据（组合引用 composed_from 可追溯）；
- 升级不改变已在运行的组合版本（引用固定 asset_id@version）。
"""

import sqlite3
import unittest

from evoagent.assets import AssetDraft
from evoagent.assets.composition import (
    AssetIO,
    ComposeError,
    check_io_compatibility,
    compose,
    dependency_closure,
)
from evoagent.assets.repository import AssetRepository


def _promote(repo, draft):
    """draft → active（带证据）。"""
    asset = draft
    for state in ("validating", "approved", "active"):
        asset = repo.transition(asset.asset_id, asset.version, state,
                                evidence_refs=("test/composition",))
    return asset


def make_repo_with_assets():
    repo = AssetRepository(sqlite3.connect(":memory:"))
    # 基础：文本抽取（产出 text）
    a_text = _promote(repo, repo.register_draft(AssetDraft(
        asset_id="skill:extract-text", asset_type="skill",
        namespace="shared_validated", version="1.0.0",
        content={"required_inputs": [], "produced_outputs": ["text"]},
        source_refs=("test/extract",))))
    # 基础：文本摘要（需要 text，产出 summary）
    a_sum = _promote(repo, repo.register_draft(AssetDraft(
        asset_id="skill:summarize", asset_type="skill",
        namespace="shared_validated", version="1.0.0",
        content={"required_inputs": ["text"],
                 "produced_outputs": ["summary"]},
        source_refs=("test/summarize",))))
    return repo, a_text, a_sum


class TestDependencyClosure(unittest.TestCase):
    def setUp(self):
        self.repo, self.a_text, self.a_sum = make_repo_with_assets()

    def test_closure_includes_self_and_deps(self):
        refs = dependency_closure(self.repo, "skill:extract-text@1.0.0")
        self.assertIn("skill:extract-text@1.0.0", refs)

    def test_missing_dependency_rejected(self):
        with self.assertRaises(ComposeError) as ctx:
            dependency_closure(self.repo, "skill:nonexistent@1.0.0")
        self.assertIn("不存在", str(ctx.exception))


class TestIOCompatibility(unittest.TestCase):
    def test_compatible_chain(self):
        up = AssetIO(produced_outputs=("text",),
                     output_types={"text": "string"})
        down = AssetIO(required_inputs=("text",),
                       input_types={"text": "string"})
        self.assertEqual(check_io_compatibility(up, down), [])

    def test_missing_input(self):
        up = AssetIO(produced_outputs=("image",))
        down = AssetIO(required_inputs=("text",))
        problems = check_io_compatibility(up, down)
        self.assertTrue(any("不产出" in p for p in problems))

    def test_type_mismatch(self):
        up = AssetIO(produced_outputs=("text",),
                     output_types={"text": "string"})
        down = AssetIO(required_inputs=("text",),
                       input_types={"text": "bytes"})
        problems = check_io_compatibility(up, down)
        self.assertTrue(any("类型不兼容" in p for p in problems))


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.repo, self.a_text, self.a_sum = make_repo_with_assets()

    def test_compose_two_assets(self):
        result = compose(self.repo, [
            "skill:extract-text@1.0.0",
            "skill:summarize@1.0.0",
        ], new_asset_id="skill:pipeline", new_version="1.0.0")
        self.assertTrue(result.asset_ref.startswith("skill:pipeline@"))
        self.assertEqual(len(result.composed_from), 2)
        self.assertIn("skill:extract-text@1.0.0", result.composed_from)
        self.assertIn("skill:summarize@1.0.0", result.composed_from)

    def test_empty_compose_rejected(self):
        with self.assertRaises(ComposeError):
            compose(self.repo, [], new_asset_id="x", new_version="1")

    def test_duplicate_refs_rejected(self):
        with self.assertRaises(ComposeError):
            compose(self.repo, [
                "skill:extract-text@1.0.0",
                "skill:extract-text@1.0.0",
            ], new_asset_id="x", new_version="1")

    def test_missing_asset_rejected(self):
        with self.assertRaises(ComposeError):
            compose(self.repo, ["skill:ghost@1.0.0"],
                    new_asset_id="x", new_version="1")

    def test_version_pinned_reference(self):
        """组合引用固定版本——升级产生新版本不改旧绑定。"""
        r1 = compose(self.repo, ["skill:extract-text@1.0.0"],
                     new_asset_id="skill:combo", new_version="1.0.0")
        r2 = compose(self.repo, ["skill:extract-text@1.0.0"],
                     new_asset_id="skill:combo", new_version="2.0.0")
        self.assertNotEqual(r1.asset_ref, r2.asset_ref)
        self.assertIn("@1.0.0", r1.asset_ref)
        self.assertIn("@2.0.0", r2.asset_ref)


if __name__ == "__main__":
    unittest.main()
