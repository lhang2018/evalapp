"""ALN-18-G 独立复核探针（固定源码 a1b3a31，zcode-main，2026-10-25）。

独立编写，不复制交付测试用例。覆盖第三批指认的重点：
快照根冻结/路径穿越/链接拒绝/排除规则持久/全路径归档身份/
注册表根绑定/真实语法失败与恢复/未知新文件不删/空文件跳过。
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WT = str(Path(__file__).resolve().parent / "wt")
sys.path.insert(0, WT)

from evoagent.tools.coding_consumption import (  # noqa: E402
    ConsumptionError, CodingPackRuntime, ProjectSnapshot,
    restore_snapshot, snapshot_project)
from evoagent.tools.registry import ToolRegistry  # noqa: E402
from evoagent.tools.coding import CODING_PACK, coding_edit_tool  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, ("| " + detail if detail else ""))


def make_link(link: Path, target: Path) -> str:
    """创建目录链接：优先真实符号链接，Windows 无权限时回退 junction。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        cmd = ["cmd", "/c", "mklink", "/J", str(link), str(target)]
        subprocess.run(cmd, check=True, capture_output=True)
        return "junction"


def remove_link(link: Path, kind: str):
    try:
        link.unlink()
    except OSError:
        try:
            os.rmdir(link)
        except OSError:
            pass


def _mkproject(files):
    root = Path(tempfile.mkdtemp(prefix="p18g_")).resolve()
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


# ---------- R1 快照根冻结与路径穿越 ----------
def r1():
    root = _mkproject({"a.py": "print(1)\n"})
    snap = snapshot_project(str(root))
    check("R1a 根冻结为绝对路径", Path(snap.root).is_absolute()
          and Path(snap.root) == root, snap.root)

    rel_snap = ProjectSnapshot("relative/root", 0.0, {"x": b"1"})
    try:
        restore_snapshot(rel_snap)
        check("R1b 相对根恢复拒绝", False)
    except ConsumptionError as exc:
        check("R1b 相对根恢复拒绝", "绝对" in str(exc), str(exc)[:80])

    evil = ProjectSnapshot(str(root), 0.0, {"../evil.txt": b"x"})
    try:
        restore_snapshot(evil)
        check("R1c ../穿越拒绝", False)
    except ConsumptionError as exc:
        check("R1c ../穿越拒绝", "非法快照相对路径" in str(exc), str(exc)[:80])

    evil2 = ProjectSnapshot(str(root), 0.0, {"a\\b.txt": b"x"})
    try:
        restore_snapshot(evil2)
        check("R1d 反斜杠穿越拒绝", False)
    except ConsumptionError:
        check("R1d 反斜杠穿越拒绝", True)

    evil3 = ProjectSnapshot(str(root), 0.0, {"sub/../../out.txt": b"x"})
    try:
        restore_snapshot(evil3)
        check("R1e 中段..穿越拒绝", False)
    except ConsumptionError:
        check("R1e 中段..穿越拒绝", True)

    evil4 = ProjectSnapshot(str(root), 0.0,
                            {str(root.parent / "out.txt"): b"x"})
    try:
        restore_snapshot(evil4)
        check("R1f 绝对路径注入拒绝", False)
    except ConsumptionError:
        check("R1f 绝对路径注入拒绝", True)


# ---------- R2 根被换成链接 / 项目内链接拒绝 ----------
def r2():
    real = _mkproject({"a.py": "print(1)\n"})
    snap = snapshot_project(str(real))
    swap = Path(tempfile.mkdtemp(prefix="p18g_swap_")).resolve()
    (swap / "b.py").write_text("print(2)\n")
    link_root = Path(str(real) + "_link")
    kind = make_link(link_root, swap)
    try:
        snap2 = ProjectSnapshot(str(link_root), snap.taken_at,
                                dict(snap.files), snap.skip_dirs,
                                snap.max_files, snap.max_bytes)
        try:
            restore_snapshot(snap2)
            check("R2a 根换链接拒绝", False)
        except ConsumptionError as exc:
            check("R2a 根换链接拒绝(" + kind + ")",
                  "链接或联接" in str(exc), str(exc)[:90])
    finally:
        remove_link(link_root, kind)

    proj = _mkproject({"a.py": "x = 1\n"})
    target = Path(tempfile.mkdtemp(prefix="p18g_tgt_")).resolve()
    kind2 = make_link(proj / "linkdir", target)
    try:
        snapshot_project(str(proj))
        check("R2b 目录符号链接快照拒绝", False, "未抛错")
    except ConsumptionError as exc:
        check("R2b 目录链接快照拒绝(" + kind2 + ")",
              "符号链接" in str(exc) or "联接" in str(exc), str(exc)[:90])
    finally:
        remove_link(proj / "linkdir", kind2)

    proj2 = _mkproject({"a.py": "x = 1\n"})
    link_file = proj2 / "lfile.py"
    try:
        os.symlink(target / "whatever", link_file)
        kf = "symlink"
    except OSError:
        print("SKIP R2c 文件符号链接（本机无符号链接权限且不适用junction）")
        kf = None
    if kf:
        try:
            snapshot_project(str(proj2))
            check("R2c 文件符号链接快照拒绝", False, "未抛错")
        except ConsumptionError:
            check("R2c 文件符号链接快照拒绝", True)
        finally:
            remove_link(link_file, kf)


# ---------- R3 排除规则持久，比较与归档同规则 ----------
def r3():
    root = _mkproject({"src/a.py": "v=1\n",
                       "node_modules/pkg/index.js": "x",
                       ".git/HEAD": "ref"})
    snap = snapshot_project(str(root))
    check("R3a 快照排除 node_modules/.git",
          "node_modules/pkg/index.js" not in snap.files
          and ".git/HEAD" not in snap.files and "src/a.py" in snap.files,
          str(sorted(snap.files)))
    (root / "node_modules" / "pkg" / "index.js").write_text("CHANGED")
    changed = snap.changed_paths(str(root))
    check("R3b 排除目录修改不计变更", changed == [], str(changed))
    (root / "node_modules" / "new.js").write_text("n")
    check("R3c 排除目录新增不计变更", snap.changed_paths(str(root)) == [])
    (root / "node_modules" / "new.js").unlink()

    root2 = _mkproject({"a.py": "1\n", "vendor/v.txt": "v"})
    snap2 = snapshot_project(str(root2), skip_dirs=("vendor",))
    check("R3d1 自定义排除进快照", "vendor/v.txt" not in snap2.files
          and snap2.skip_dirs == ("vendor",))
    (root2 / "vendor" / "v.txt").write_text("changed")
    check("R3d 自定义排除持久到比较", snap2.changed_paths(str(root2)) == [])


# ---------- R4 上限与跨根 ----------
def r4():
    root = _mkproject({f"f{i}.txt": "x" for i in range(5)})
    try:
        snapshot_project(str(root), max_files=3)
        check("R4a 文件数上限拒绝", False, "未抛错")
    except ConsumptionError as exc:
        check("R4a 文件数上限拒绝", "上限" in str(exc), str(exc)[:80])
    try:
        snapshot_project(str(root), max_files=0)
        check("R4b 非正上限拒绝", False)
    except ConsumptionError:
        check("R4b 非正上限拒绝", True)
    big = _mkproject({"big.bin": b"\x01" * 300})
    try:
        snapshot_project(str(big), max_bytes=200)
        check("R4c 字节上限拒绝", False, "未抛错")
    except ConsumptionError as exc:
        check("R4c 字节上限拒绝", "上限" in str(exc), str(exc)[:80])
    root_b = _mkproject({"f0.txt": "x"})
    snap = snapshot_project(str(root))
    try:
        snap.changed_paths(str(root_b))
        check("R4d 跨根比较拒绝", False)
    except ConsumptionError as exc:
        check("R4d 跨根比较拒绝", "不一致" in str(exc), str(exc)[:80])


# ---------- R5 全路径归档身份与恢复语义 ----------
class FakeStore:
    def __init__(self):
        self.saved = {}

    def save(self, task_id, artifact_id, kind, content, description=""):
        self.saved[artifact_id] = (task_id, kind, bytes(content))
        return {"artifact_id": artifact_id, "kind": kind}


def r5():
    root = _mkproject({"a/one.py": "v1\n", "b/one.py": "v1\n"})
    snap = snapshot_project(str(root))
    rt = CodingPackRuntime(ToolRegistry(), artifact_store=None)
    out = rt.archive_modified("t1", str(root), snap)
    check("R5a 无 ArtifactStore 明确 skipped",
          out["skipped"] == ["no_artifact_store"] and out["archived"] == [],
          str(out))

    store = FakeStore()
    rt2 = CodingPackRuntime(ToolRegistry(), artifact_store=store)
    (root / "a" / "one.py").write_text("v2\n")
    (root / "b" / "one.py").write_text("v3\n")
    (root / "empty.txt").write_text("")
    out2 = rt2.archive_modified("t1", str(root), snap)
    ids = {e["artifact_id"] for e in out2["archived"]}
    check("R5b 不同目录同名文件不碰撞", len(ids) == 2, str(ids))
    check("R5c 归档身份=全相对路径摘要",
          all(e["artifact_id"] == "file_" + hashlib.sha256(
              e["source_path"].encode()).hexdigest()
              for e in out2["archived"]))
    check("R5d 空文件按 skip:empty", "empty.txt:empty" in out2["skipped"],
          str(out2["skipped"]))
    (root / "b" / "one.py").unlink()
    (root / "new.txt").write_text("new")
    out3 = rt2.archive_modified("t1", str(root), snap)
    check("R5e 删除文件 skip:deleted", "b/one.py:deleted" in out3["skipped"],
          str(out3["skipped"]))
    check("R5f 新增文件被归档",
          any(e["source_path"] == "new.txt" for e in out3["archived"]),
          str([e["source_path"] for e in out3["archived"]]))


# ---------- R6 恢复：预检/重建/未知文件不删 ----------
def r6():
    root = _mkproject({"keep.py": "k=1\n", "gone/g.py": "g=1\n"})
    snap = snapshot_project(str(root))
    (root / "keep.py").write_text("k=2\n")
    (root / "gone" / "g.py").unlink()
    (root / "newfile.py").write_text("n=1\n")
    touched = restore_snapshot(snap)
    check("R6a 修改文件恢复原内容", (root / "keep.py").read_text() == "k=1\n")
    check("R6b 删除文件可重建", (root / "gone" / "g.py").exists()
          and (root / "gone" / "g.py").read_text() == "g=1\n")
    check("R6c 未知新文件不删除", (root / "newfile.py").exists()
          and "newfile.py" not in touched, str(touched))
    check("R6d touched 只含实际恢复项",
          touched == ["gone/g.py", "keep.py"], str(touched))

    root2 = _mkproject({"a.txt": "A\n", "z.txt": "Z\n"})
    snap2 = snapshot_project(str(root2))
    (root2 / "z.txt").unlink()
    (root2 / "z.txt").mkdir()
    (root2 / "z.txt" / "inner.txt").write_text("i")
    try:
        restore_snapshot(snap2)
        check("R6e 路径变目录拒绝", False, "未抛错")
    except ConsumptionError as exc:
        check("R6e 路径变目录拒绝", "变成目录" in str(exc), str(exc)[:90])
        check("R6f 拒绝发生在破坏前", (root2 / "a.txt").read_text() == "A\n"
              and (root2 / "z.txt" / "inner.txt").exists())


# ---------- R7 注册表根绑定 ----------
def r7():
    root_a = _mkproject({"a.py": "A\n"})
    root_b = _mkproject({"a.py": "B\n"})

    def fresh():
        return CodingPackRuntime(ToolRegistry())

    rt = fresh()
    b1 = rt.install_for_task("t1", str(root_a))
    check("R7a 绑定记录含精确身份",
          b1["pack_id"] == CODING_PACK.pack_id
          and b1["root"] == str(root_a)
          and sorted(b1["tool_ids"]) == sorted(CODING_PACK.tool_ids)
          and b1["task_id"] == "t1",
          str({k: b1[k] for k in ("pack_id", "root", "task_id")}))
    b2 = rt.install_for_task("t2", str(root_a))
    check("R7b 同根重复安装幂等", b2["root"] == str(root_a))
    try:
        rt.install_for_task("t3", str(root_b))
        check("R7c 跨根拒绝", False, "未抛错")
    except ConsumptionError as exc:
        check("R7c 跨根拒绝", "其他项目" in str(exc), str(exc)[:90])

    rt_x = fresh()
    rt_x.install_for_task("t1", str(root_a))
    reg = rt_x.registry
    old = reg.contract("coding_edit")
    tampered = type(old)(**{**{f: getattr(old, f) for f in
                               old.__dataclass_fields__},
                             "tool_version": old.tool_version + "-tampered"})
    reg.register(tampered, lambda args: None)
    try:
        rt_x.install_for_task("t4", str(root_a))
        check("R7d 契约变化拒绝沿用", False, "未抛错")
    except ConsumptionError as exc:
        check("R7d 契约变化拒绝沿用", "已变化" in str(exc), str(exc)[:90])

    rt_y = fresh()
    rt_y.registry.register(*coding_edit_tool(str(root_a)))
    try:
        rt_y.install_for_task("t5", str(root_a))
        check("R7e 存量编码工具拒覆盖", False, "未抛错")
    except ConsumptionError as exc:
        check("R7e 存量编码工具拒覆盖", "缺少项目绑定" in str(exc), str(exc)[:90])


# ---------- R8 真实语法失败与恢复 E2E（真实执行内核） ----------
def r8():
    from evoagent.tools.coding import coding_syntax_check_tool
    root = _mkproject({"good.py": "def f():\n    return 1\n",
                       "bad.py": "def broken():\n    pass\n"})
    reg = ToolRegistry()
    rt = CodingPackRuntime(reg)
    rt.install_for_task("t-e2e", str(root))
    _contract, runner = coding_syntax_check_tool(str(root))
    # 1) 先对好状态快照
    snap = snapshot_project(str(root))
    check("R8pre 快照时点语法通过",
          runner({"targets": ["."]}).evidence.get("returncode") == 0)
    # 2) 引入真实语法破损 → compileall 非零 returncode 且 executed=True
    (root / "bad.py").write_text("def broken(:\n    pass\n")
    broken = runner({"targets": ["bad.py"]})
    check("R8a 破损文件 executed=True", broken.executed is True)
    check("R8b returncode 非零且记录",
          broken.evidence.get("returncode") not in (0, None)
          and broken.ok is False and broken.goal_reached is False,
          f"rc={broken.evidence.get('returncode')} ok={broken.ok} "
          f"err_class={broken.error_class}")
    check("R8b2 好文件不受影响", runner(
        {"targets": ["good.py"]}).evidence.get("returncode") == 0)
    # 3) 快照回滚 → 语法恢复 → 再次运行 returncode=0
    touched = restore_snapshot(snap)
    after = runner({"targets": ["."]})
    check("R8d 回滚后语法检查恢复通过",
          after.ok is True and after.evidence.get("returncode") == 0,
          f"rc={after.evidence.get('returncode')} touched={touched}")
    check("R8e 回滚内容与快照一致",
          (root / "bad.py").read_text() == "def broken():\n    pass\n")


def main():
    for fn in (r1, r2, r3, r4, r5, r6, r7, r8):
        print(f"---- {fn.__name__} ----")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(fn.__name__ + "(整组执行)", False,
                  f"{type(exc).__name__}: {exc}")
    fails = [r for r in results if not r[1]]
    print(f"\n=== 18-G 探针：{len(results) - len(fails)}/{len(results)} 通过 ===")
    for name, _, detail in fails:
        print("FAIL:", name, "|", detail)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
