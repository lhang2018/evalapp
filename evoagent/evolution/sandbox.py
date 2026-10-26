"""候选代码隔离执行（PRD 47.4 / Architecture 18.3）。

候选在独立子进程中运行：
- 工作目录为一次性临时沙箱；
- 写入/删除只允许发生在沙箱内；读取限制在沙箱与 Python 运行时自身；
- 网络、子进程、动态库加载与进程派生被审计钩子阻断；
- 环境变量白名单，生产凭据不继承；
- 超时强杀、输出截断。

注意：这是「降低风险的受控执行环境」，不是针对恶意代码的强安全边界
（Python 审计钩子无法覆盖解释器之外的行为）。候选不能默认继承生产权限。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT_S = 5.0
MAX_TIMEOUT_S = 30.0
DEFAULT_OUTPUT_LIMIT = 64 * 1024
MAX_OUTPUT_LIMIT = 256 * 1024

_ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "LANG", "LC_ALL",
}

_BOOTSTRAP = r'''
import os
import runpy
import sys
import traceback

SANDBOX = os.path.realpath(os.environ["EVOAGENT_SANDBOX_ROOT"])
READ_ROOTS = [SANDBOX]
for p in [sys.prefix, sys.base_prefix, os.path.dirname(os.__file__)] + list(sys.path):
    if p and os.path.isdir(p):
        try:
            READ_ROOTS.append(os.path.realpath(p))
        except OSError:
            pass
WRITE_EVENTS = {"os.remove", "os.mkdir", "os.rmdir", "os.chmod", "os.chown",
                "os.truncate", "os.utime", "os.symlink", "os.link", "os.rename"}
BLOCKED_PREFIX = ("os.exec", "os.spawn", "os.posix_spawn")
BLOCKED = {"socket.connect", "socket.getaddrinfo", "socket.bind",
           "socket.gethostbyname", "socket.gethostbyaddr",
           "subprocess.Popen", "os.system", "os.fork", "os.forkpty",
           "ctypes.dlopen", "ctypes.dlsym", "ctypes.dlsym/handle"}
_in_hook = False


class SandboxViolation(PermissionError):
    pass


def _realpath(path):
    try:
        return os.path.realpath(os.fspath(path))
    except (TypeError, ValueError, OSError):
        return ""


def _inside(path, roots):
    p = _realpath(path)
    if not p:
        return False
    for root in roots:
        if p == root or p.startswith(root + os.sep):
            return True
    return False


def _writing(mode, flags):
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    if isinstance(flags, int):
        mask = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return bool(flags & mask)
    return False


def _hook(event, args):
    global _in_hook
    if _in_hook:
        return
    _in_hook = True
    try:
        if event in BLOCKED or event.startswith(BLOCKED_PREFIX):
            raise SandboxViolation("blocked: " + event)
        if event == "open":
            path = args[0] if args else None
            if path is None or isinstance(path, int):
                return
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            if _writing(mode, flags):
                if not _inside(path, [SANDBOX]):
                    raise SandboxViolation(
                        "write outside sandbox: " + str(path))
            elif not _inside(path, READ_ROOTS):
                raise SandboxViolation("read outside sandbox: " + str(path))
        elif event in WRITE_EVENTS:
            # Relative paths plus dir_fd refer to a different directory than
            # realpath(cwd/path). Do not authorize descriptor-based mutations
            # without a path that this portable hook can actually verify.
            if event != "os.truncate":
                descriptors = args[-2:] if event in ("os.rename", "os.link") else args[-1:]
                if any(fd not in (None, -1) for fd in descriptors):
                    raise SandboxViolation(event + " directory descriptor is not allowed")
            targets = args[:2] if event in ("os.rename", "os.symlink",
                                            "os.link") else args[:1]
            for target in targets:
                if not isinstance(target, (str, bytes, os.PathLike)) \
                        or not _inside(target, [SANDBOX]):
                    raise SandboxViolation(
                        event + " outside sandbox: " + str(target))
        elif event in ("os.listdir", "os.scandir", "os.chdir"):
            target = args[0] if args else None
            if isinstance(target, (str, bytes, os.PathLike)) \
                    and not _inside(target, READ_ROOTS):
                raise SandboxViolation(
                    event + " outside sandbox: " + str(target))
    finally:
        _in_hook = False


def _record(message):
    with open(os.path.join(SANDBOX, "_violations.txt"), "a",
              encoding="utf-8") as fh:
        fh.write(message + "\n")


sys.addaudithook(_hook)
ENTRY = os.environ.get("EVOAGENT_SANDBOX_ENTRY") or "candidate.py"
candidate = os.path.join(SANDBOX, ENTRY)
# 真实候选资产可能是多文件项目：把沙箱目录加入导入路径，
# 使入口文件可以 import 同目录下的候选模块（仍然只能读沙箱内文件）。
sys.path.insert(0, SANDBOX)
sys.argv = [candidate]
try:
    runpy.run_path(candidate, run_name="__main__")
except SystemExit:
    raise
except SandboxViolation as exc:
    _record(str(exc))
    traceback.print_exc()
    sys.exit(97)
except BaseException:
    traceback.print_exc()
    sys.exit(1)
'''


@dataclass
class SandboxResult:
    status: str                    # passed | failed | timeout | violation
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    violations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "exit_code": self.exit_code,
                "stdout": self.stdout, "stderr": self.stderr,
                "duration_s": self.duration_s,
                "violations": self.violations}


class SandboxRunner:
    """在一次性目录中运行候选代码；所有限制由本地进程执行。"""

    def __init__(self, python: Optional[str] = None):
        self.python = python or sys.executable

    def run_code(self, code: str, timeout: float = DEFAULT_TIMEOUT_S,
                 output_limit: int = DEFAULT_OUTPUT_LIMIT) -> SandboxResult:
        timeout = _clamp(timeout, 0.1, MAX_TIMEOUT_S)
        output_limit = _clamp(output_limit, 256, MAX_OUTPUT_LIMIT)
        root = Path(tempfile.mkdtemp(prefix="evoagent_sandbox_"))
        try:
            (root / "candidate.py").write_text(code or "", encoding="utf-8")
            (root / "_runner.py").write_text(_BOOTSTRAP, encoding="utf-8")
            return self._execute(root, timeout, int(output_limit))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def run_file(self, path: str, timeout: float = DEFAULT_TIMEOUT_S,
                 output_limit: int = DEFAULT_OUTPUT_LIMIT) -> SandboxResult:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"候选文件不存在: {path}")
        return self.run_code(source.read_text(encoding="utf-8"),
                             timeout=timeout, output_limit=output_limit)

    def run_project(self, files: Dict[str, str], entry: str = "candidate.py",
                    timeout: float = DEFAULT_TIMEOUT_S,
                    output_limit: int = DEFAULT_OUTPUT_LIMIT) -> SandboxResult:
        """把多文件候选资产写入一次性目录后执行入口文件。

        供 ALN-09-H 的隔离候选宿主使用：资产正文先落盘再执行，超时强杀、
        网络/子进程阻断、凭据不继承等边界与 ``run_code`` 完全相同。
        路径必须是沙箱内相对路径（拒绝绝对路径、盘符与 ``..``），入口文件
        必须存在于 ``files`` 中，否则拒绝执行而不是猜一个入口。
        """
        if not isinstance(files, dict) or not files:
            raise ValueError("候选资产必须提供非空的 files 映射")
        entry = entry or "candidate.py"
        timeout = _clamp(timeout, 0.1, MAX_TIMEOUT_S)
        output_limit = _clamp(output_limit, 256, MAX_OUTPUT_LIMIT)
        root = Path(tempfile.mkdtemp(prefix="evoagent_sandbox_"))
        try:
            for relative, text in files.items():
                if not isinstance(text, str):
                    raise ValueError(f"候选文件内容必须是文本: {relative!r}")
                target = _materialize(root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            if not _materialize(root, entry).is_file():
                raise FileNotFoundError(f"候选入口文件不存在: {entry}")
            (root / "_runner.py").write_text(_BOOTSTRAP, encoding="utf-8")
            return self._execute(root, timeout, int(output_limit), entry=entry)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _execute(self, root: Path, timeout: float,
                 output_limit: int, entry: str = "candidate.py") -> SandboxResult:
        env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
        env.update({
            "EVOAGENT_SANDBOX_ROOT": str(root),
            "EVOAGENT_SANDBOX_ENTRY": entry or "candidate.py",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "TEMP": str(root),
            "TMP": str(root),
        })
        started = time.time()
        timed_out = False
        try:
            proc = subprocess.Popen(
                [self.python, "-I", "-B", str(root / "_runner.py")],
                cwd=str(root), env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            return SandboxResult(status="failed", stderr=str(e),
                                 duration_s=round(time.time() - started, 3))
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            out, err = proc.communicate()
        duration = round(time.time() - started, 3)
        violations = _read_violations(root)
        if violations:
            status = "violation"
        elif timed_out:
            status = "timeout"
        elif proc.returncode == 0:
            status = "passed"
        else:
            status = "failed"
        return SandboxResult(
            status=status, exit_code=proc.returncode,
            stdout=_decode(out, output_limit),
            stderr=_decode(err, output_limit),
            duration_s=duration, violations=violations)


def _materialize(root: Path, relative: str) -> Path:
    """把候选资产里的相对路径解析到沙箱目录内；越界路径直接拒绝。"""
    if not isinstance(relative, str) or not relative:
        raise ValueError("候选文件路径不得为空")
    text = relative.replace("\\", "/")
    if text.startswith("/") or ":" in text:
        raise ValueError(f"候选文件路径不得为绝对路径: {relative!r}")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"候选文件路径非法: {relative!r}")
    base = os.path.realpath(root)
    resolved = os.path.realpath(root.joinpath(*parts))
    if not (resolved == base or resolved.startswith(base + os.sep)):
        raise ValueError(f"候选文件路径越界: {relative!r}")
    return root.joinpath(*parts)


def _read_violations(root: Path) -> List[str]:
    path = root / "_violations.txt"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line for line in lines if line.strip()][:20]


def _decode(raw: bytes, limit: int) -> str:
    text = (raw or b"").decode("utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + f"\n...（已截断，共 {len(text)} 字符）"
    return text


def _clamp(value: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))
