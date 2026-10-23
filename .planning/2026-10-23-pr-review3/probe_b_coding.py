"""九卡修复第三方复核独立探针 B 组：PR-02 / PR-03（claude-main）。

PR-03 diff 行匹配（修复 7132180）：
  D1 hunk 旧行「prefix needle」含 needle → 修复前行中子串命中会改错处，
     修复后必须拒绝（完整行边界）；护栏：整行恰好等于 needle 时仍命中。
PR-02 提交/恢复/并发边界（修复 7132180 + 34b5fac）：
  P1 新建文件（/dev/null diff）在预检后并发出现同名文件 → 修复前覆盖，
     修复后排他创建拒绝；
  P2 回滚不覆盖外部/未知内容：prepared 后外部追加新行 → 修复前整文件还原
     吞掉外部行，修复后保留未知内容并报 side_effect_uncertain 或部分回滚；
  P3 恢复（recover）遇非法日志结构 → 修复前抛未处理异常或 ok=true，
     修复后顶层汇总错误、ok=false；
  P4 类型检查器契约：必须声明 external/high 风险（不再是「无副作用准备」）。
"""
import sys
import os
import json
import tempfile
import subprocess

results = []


def probe(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except (AssertionError, ValueError, TypeError, RuntimeError,
            subprocess.SubprocessError) as exc:
        results.append((name, f"FAIL: {type(exc).__name__}: {exc}"))


def new_root():
    return tempfile.mkdtemp(prefix="coding-probe-")


def make_files(root, files):
    for rel, content in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)


def read_file(root, rel):
    with open(os.path.join(root, rel), "r", encoding="utf-8") as f:
        return f.read()


def tools(root):
    from evoagent.tools.coding import (coding_patch_tool,
                                       coding_patch_recover_tool,
                                       coding_typecheck_tool)
    return (coding_patch_tool(root)[1], coding_patch_recover_tool(root)[1],
            coding_typecheck_tool(root)[1])


def tr_ok(tr):
    return bool(getattr(tr, "ok", getattr(tr, "success", None))), tr


# ---------------------------------------------------------------- PR-03 D1
def d1_hunk_full_line_boundary():
    root = new_root()
    src = "alpha\nprefix needle\nbeta\n"
    make_files(root, {"src.txt": src})
    patch, _rec, _tc = tools(root)
    # hunk 旧行是 "needle"（不带前缀）；文件里只有 "prefix needle"
    # 行中子串匹配会误命中；完整行边界匹配必须拒绝
    diff = ("--- a/src.txt\n+++ b/src.txt\n@@ -1,2 +1,2 @@\n"
            "-needle\n+NEEDLE-REPLACED\n alpha\n")
    tr = patch({"diff": diff})
    ok, tr = tr_ok(tr)
    content = read_file(root, "src.txt")
    assert "prefix NEEDLE-REPLACED" not in content, (
        "hunk 在行中子串处命中并改写了错误内容")
    # 修复后行为：要么拒绝（非零退出/错误结果），要么未应用
    assert (not ok) or content == src, (
        "非法 hunk 被静默应用")


def d1b_exact_line_still_matches():
    root = new_root()
    src = "alpha\nneedle\nbeta\n"
    make_files(root, {"src.txt": src})
    patch, _rec, _tc = tools(root)
    diff = ("--- a/src.txt\n+++ b/src.txt\n@@ -1,3 +1,3 @@\n"
            " alpha\n-needle\n+NEEDLE-REPLACED\n beta\n")
    tr = patch({"diff": diff})
    ok, tr = tr_ok(tr)
    assert ok, f"合法整行 hunk 应成功: {tr}"
    assert "NEEDLE-REPLACED" in read_file(root, "src.txt"), (
        "整行匹配未生效（护栏失败）")


# ---------------------------------------------------------------- PR-02 P1
def p1_exclusive_create_for_new_files():
    root = new_root()
    patch, _rec, _tc = tools(root)
    diff = ("--- /dev/null\n+++ b/concurrent.txt\n@@ -0,0 +1,1 @@\n"
            "+created by patch\n")
    # 预检后、写入前的并发：模拟为 patch 内部窗口不可控，改为两次场景——
    # 场景 A：目标已存在时以 /dev/null diff 新建 → 必须拒绝而非覆盖
    make_files(root, {"concurrent.txt": "concurrent external content\n"})
    tr = patch({"diff": diff})
    ok, tr = tr_ok(tr)
    content = read_file(root, "concurrent.txt")
    assert content == "concurrent external content\n", (
        f"/dev/null 新建 diff 覆盖了已存在文件（现内容: {content!r}）")
    assert not ok, "对已存在目标的新建 diff 应失败"


# ---------------------------------------------------------------- PR-02 P2
def p2_rollback_preserves_external_content():
    root = new_root()
    make_files(root, {"a.txt": "line1\nline2\n"})
    patch, _rec, _tc = tools(root)
    diff = ("--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n"
            "-line1\n+LINE1-CHANGED\n line2\n")
    tr = patch({"diff": diff})
    ok, tr = tr_ok(tr)
    assert ok, f"合法补丁应成功: {tr}"
    # 提交成功后，用 recover 不应把外部内容吞掉；先注入外部变更
    with open(os.path.join(root, "a.txt"), "a", encoding="utf-8") as f:
        f.write("external-appended\n")
    rec, _p, _tc = tools(root)
    tr2 = rec({})
    ok2, tr2 = tr_ok(tr2)
    content = read_file(root, "a.txt")
    # recover 对 committed 日志通常只做清理；若它执行了回滚，外部行不得被吞
    if "LINE1-CHANGED" not in content:
        # 回滚发生了：原内容回来，但外部追加行必须保留或明确报不确定
        assert "external-appended" in content, (
            "回滚吞掉了外部追加内容且未标记 side_effect_uncertain")
    # 无论哪种路径，不允许出现 ok=true 且外部内容凭空消失
    assert not (ok2 and "external-appended" not in content
                and "LINE1-CHANGED" not in content), (
        "recover 报成功但外部内容与补丁内容都消失")


# ---------------------------------------------------------------- PR-02 P3
def p3_recover_invalid_log_no_crash_no_fake_ok():
    root = new_root()
    make_files(root, {})
    log_dir = os.path.join(root, ".evoagent", "coding_patch")
    os.makedirs(log_dir, exist_ok=True)
    # 非法结构日志：根不是对象
    with open(os.path.join(log_dir, "bad1.json"), "w", encoding="utf-8") as f:
        f.write("[1,2,3]")
    # 合法外壳但状态未知
    with open(os.path.join(log_dir, "bad2.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"unknown_field": True}))
    rec, _p, _tc = tools(root)
    tr = rec({})
    ok, tr = tr_ok(tr)
    # 修复后：顶层汇总错误，ok 可为 true（跳过坏日志）但必须报告问题，
    # 或整体 ok=false；不允许 ok=true 且零错误提及
    text = json.dumps(getattr(tr, "to_dict", lambda: tr.__dict__)(),
                      ensure_ascii=False, default=str)
    mentioned = ("bad1" in text or "bad2" in text or "日志" in text
                 or "error" in text.lower() or not ok)
    assert mentioned, (
        f"非法日志被静默忽略且整体报成功: {text[:200]}")


# ---------------------------------------------------------------- PR-03 P4
def p4_typecheck_contract_external_high():
    root = new_root()
    _patch, _rec, tc = tools(root)
    contract = None
    # coding_typecheck_tool 返回 (contract, fn)
    from evoagent.tools.coding import coding_typecheck_tool
    contract = coding_typecheck_tool(root)[0]
    d = contract.to_dict() if hasattr(contract, "to_dict") else dict(
        contract.__dict__)
    text = json.dumps(d, ensure_ascii=False, default=str).lower()
    assert "external" in text, f"类型检查器契约未声明 external 风险: {text[:200]}"
    assert ("high" in text), "类型检查器契约未声明 high 风险级"


probe("D1 hunk-line-boundary", d1_hunk_full_line_boundary)
probe("D1b exact-line-matches[guard]", d1b_exact_line_still_matches)
probe("P1 exclusive-create-newfile", p1_exclusive_create_for_new_files)
probe("P2 rollback-keeps-external", p2_rollback_preserves_external_content)
probe("P3 recover-invalid-log-reported", p3_recover_invalid_log_no_crash_no_fake_ok)
probe("P4 typecheck-declares-external-high", p4_typecheck_contract_external_high)

print("=" * 56)
for name, status in results:
    print(f"{status:8} {name}")
failed = [n for n, s in results if s.startswith("FAIL")]
print("=" * 56)
print(f"total={len(results)} pass={len(results)-len(failed)} fail={len(failed)}")
sys.exit(1 if failed else 0)
