#!/usr/bin/env python
"""真机提交任务编排：启动→输入→提交→验证（用于 12-V 矩阵）。"""
import json, subprocess, sys, time
from lxml import etree

ADB = ["adb", "-s", "5748d823"]
TMP = r"E:\evalapp\.planning\2026-10-25-aln12v\_ui_submit.xml"

def sh(*args, timeout=60):
    return subprocess.run(ADB + list(args), capture_output=True, text=True, timeout=timeout)

def dump():
    sh("shell", "uiautomator", "dump", "/sdcard/_ui_submit.xml")
    subprocess.run(ADB + ["pull", "/sdcard/_ui_submit.xml", TMP], capture_output=True, check=True)
    return etree.parse(TMP)

def center(bounds):
    left, top, right, bottom = [int(v) for v in bounds.replace("][", ",").strip("[]").split(",")]
    return (left + right) // 2, (top + bottom) // 2

def find(tree, *, desc=None, cls=None, hint=None, min_y=0):
    for node in tree.iter("node"):
        if cls and node.get("class") != cls:
            continue
        if desc is not None and (node.get("content-desc") or "").strip() != desc:
            continue
        if hint is not None and hint not in (node.get("hint") or ""):
            continue
        bounds = node.get("bounds") or ""
        try:
            x, y = center(bounds)
        except Exception:
            continue
        if y < min_y:
            continue
        return node, (x, y)
    return None, None

def tap(point):
    sh("shell", "input", "tap", str(point[0]), str(point[1]))

def foreground():
    out = sh("shell", "dumpsys", "window").stdout
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            return "com.evoagent" in line
    return False

def queue_line(tree):
    for node in tree.iter("node"):
        text = node.get("text") or ""
        if text.startswith("待上传"):
            return text
    return ""

def main():
    text = sys.argv[1]
    sh("shell", "am", "start", "-n", "com.evoagent/.MainActivity")
    time.sleep(3)
    tree = dump()
    node, point = find(tree, cls="android.widget.EditText", hint="输入任务目标")
    if node is None:
        node, point = find(tree, cls="android.widget.EditText", min_y=600)
    if node is None:
        print(json.dumps({"ok": False, "stage": "input_not_found"})); return 1
    tap(point); time.sleep(1)
    sh("shell", "input", "text", text); time.sleep(1)
    node, point = find(tree, desc="提交任务", min_y=400)
    if node is None or node.get("enabled") != "true":
        sh("shell", "input", "keyevent", "4"); time.sleep(1)
        tree = dump()
        node, point = find(tree, desc="提交任务", min_y=400)
    if node is None:
        print(json.dumps({"ok": False, "stage": "submit_not_found", "foreground": foreground()})); return 1
    tap(point); time.sleep(6)
    tree = dump()
    lines = [n.get("text") for n in tree.iter("node") if n.get("text")]
    has_text = any(text in (line or "") for line in lines)
    result = {"ok": True, "queue": queue_line(tree), "task_visible": has_text,
              "foreground": foreground(),
              "task_lines": [l for l in lines if text.split()[0] in (l or "")][:3]}
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
