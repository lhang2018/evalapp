"""Git 发布分支策略（PRD 59-60 / 62-63）：隔离分支、标签、快进合并。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from evoagent.release.git_flow import (GitFlowError, GitReleaseFlow,
                                       validate_branch_name,
                                       validate_tag_name)


def _git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _commit(repo, name, content="content"):
    Path(repo, name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", f"add {name}")


class TestGitFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_gitflow_")
        self.repo = Path(self.tmp)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)],
                       check=True, capture_output=True)
        _commit(self.repo, "base.txt")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_and_branch_creation(self):
        flow = GitReleaseFlow(str(self.repo))
        status = flow.status()
        self.assertEqual(status["branch"], "main")
        self.assertTrue(status["clean"])
        self.assertEqual(len(status["commit"]), 40)

        created = flow.create_branch("stable/v1.0.0")
        self.assertEqual(created["branch"], "stable/v1.0.0")
        self.assertTrue(created["clean"])

    def test_name_validation(self):
        with self.assertRaises(GitFlowError):
            validate_branch_name("feature/x")
        with self.assertRaises(GitFlowError):
            validate_branch_name("stable/")
        with self.assertRaises(GitFlowError):
            validate_branch_name("stable/../escape")
        with self.assertRaises(GitFlowError):
            validate_tag_name("-bad")
        self.assertEqual(validate_branch_name("exp/model-a"),
                         "exp/model-a")

    def test_dirty_tree_blocks_branch_and_merge(self):
        flow = GitReleaseFlow(str(self.repo))
        Path(self.repo, "scratch.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaises(GitFlowError):
            flow.create_branch("stable/v1.0.0")
        with self.assertRaises(GitFlowError):
            flow.merge_to_main("stable/v1.0.0")

    def test_fast_forward_merge_to_main(self):
        flow = GitReleaseFlow(str(self.repo))
        flow.create_branch("stable/v1.0.0")
        _commit(self.repo, "release.txt", "release")
        release_head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "main")

        result = flow.merge_to_main("stable/v1.0.0")
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["merged"], "stable/v1.0.0")
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), release_head)
        self.assertTrue(Path(self.repo, "release.txt").exists())

    def test_diverged_merge_fails_and_keeps_main(self):
        flow = GitReleaseFlow(str(self.repo))
        flow.create_branch("stable/v1.0.0")
        _commit(self.repo, "release.txt")
        _git(self.repo, "checkout", "main")
        _commit(self.repo, "main_only.txt")
        main_head = _git(self.repo, "rev-parse", "HEAD")

        with self.assertRaises(GitFlowError):
            flow.merge_to_main("stable/v1.0.0")
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), main_head)
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")
        self.assertFalse(Path(self.repo, "release.txt").exists())

    def test_tag_creation(self):
        flow = GitReleaseFlow(str(self.repo))
        head = _git(self.repo, "rev-parse", "HEAD")
        result = flow.tag("v1.0.0")
        self.assertEqual(result["commit"], head)
        self.assertEqual(
            _git(self.repo, "rev-parse", "v1.0.0^{commit}"), head)
        with self.assertRaises(GitFlowError):
            flow.tag("v1.0.0")

    def test_non_repository_rejected(self):
        plain = Path(tempfile.mkdtemp(prefix="evoagent_plain_"))
        try:
            with self.assertRaises(GitFlowError):
                GitReleaseFlow(str(plain))
        finally:
            shutil.rmtree(plain, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
