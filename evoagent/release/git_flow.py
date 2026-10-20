"""Git 发布分支策略（PRD 59-60 / 62-63）。

只封装发布所需的最小操作：状态、创建隔离分支、打标签、快进合并到 main。
- 分支前缀限于 PRD 60 建议集合，避免任意分支写入。
- 合并只允许 `--ff-only` 到 main；分叉或冲突直接失败，不自动解决。
- 工作区不干净时拒绝切换/合并，避免覆盖未提交改动。
- 全部为显式调用；运行时不会自动触发，也不读取/写入 git 凭据。
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


class GitFlowError(RuntimeError):
    pass


BRANCH_PREFIXES = ("stable/", "adapt/", "exp/", "capability/", "shared/")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
_MERGE_TARGET = "main"


def validate_branch_name(name: str) -> str:
    if not any(name.startswith(prefix) for prefix in BRANCH_PREFIXES):
        raise GitFlowError(
            f"分支必须以 {', '.join(BRANCH_PREFIXES)} 之一开头")
    rest = name.split("/", 1)[1]
    if ".." in name or not _NAME_RE.fullmatch(rest):
        raise GitFlowError(f"非法分支名: {name}")
    return name


def validate_tag_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise GitFlowError(f"非法标签名: {name}")
    return name


class GitReleaseFlow:
    def __init__(self, repo_path: str, timeout_seconds: int = 30):
        self.repo = Path(repo_path).resolve()
        self._timeout = timeout_seconds
        code, _ = self._git(["rev-parse", "--git-dir"], check=False)
        if code != 0:
            raise GitFlowError(f"不是 Git 仓库: {self.repo}")

    def _git(self, args: List[str], check: bool = True):
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.repo), *args],
                capture_output=True, text=True,
                timeout=self._timeout, check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitFlowError(f"git 执行失败: {exc}") from exc
        if check and proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip()
            raise GitFlowError(message[:500] or f"git {' '.join(args)} 失败")
        return proc.returncode, proc.stdout.strip()

    def status(self) -> Dict[str, Any]:
        code, branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"],
                                 check=False)
        if code != 0:
            raise GitFlowError("无法读取当前分支")
        _, commit = self._git(["rev-parse", "HEAD"])
        _, porcelain = self._git(["status", "--porcelain"])
        return {"repo": str(self.repo), "branch": branch,
                "commit": commit, "clean": not porcelain}

    def _branch_exists(self, name: str) -> bool:
        code, _ = self._git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            check=False)
        return code == 0

    def create_branch(self, branch: str, base: str = "HEAD") -> Dict[str, Any]:
        validate_branch_name(branch)
        if branch == _MERGE_TARGET:
            raise GitFlowError("不能覆盖 main")
        if self._branch_exists(branch):
            raise GitFlowError(f"分支已存在: {branch}")
        status = self.status()
        if not status["clean"]:
            raise GitFlowError("工作区不干净，拒绝创建发布分支")
        _, base_commit = self._git(["rev-parse", "--verify", f"{base}^{{commit}}"])
        self._git(["checkout", "-b", branch, base_commit])
        return self.status()

    def tag(self, name: str, ref: str = "HEAD") -> Dict[str, Any]:
        """轻量标签：不需要 git 身份，避免隐式写入身份配置。"""
        validate_tag_name(name)
        _, commit = self._git(["rev-parse", "--verify", f"{ref}^{{commit}}"])
        if self._git(["rev-parse", "--verify", "--quiet",
                      f"refs/tags/{name}"], check=False)[0] == 0:
            raise GitFlowError(f"标签已存在: {name}")
        self._git(["tag", name, commit])
        return {"tag": name, "commit": commit}

    def merge_to_main(self, branch: str) -> Dict[str, Any]:
        """仅允许把隔离分支快进合并到 main；不产生合并提交。"""
        validate_branch_name(branch)
        if branch == _MERGE_TARGET:
            raise GitFlowError("不能合并 main 到自身")
        if not self._branch_exists(branch):
            raise GitFlowError(f"分支不存在: {branch}")
        status = self.status()
        if not status["clean"]:
            raise GitFlowError("工作区不干净，拒绝合并")
        previous = status["branch"]
        self._git(["checkout", _MERGE_TARGET])
        try:
            self._git(["merge", "--ff-only", branch])
        except GitFlowError:
            if previous != _MERGE_TARGET:
                self._git(["checkout", previous], check=False)
            raise
        result = self.status()
        result.update({"merged": branch, "target": _MERGE_TARGET})
        return result
