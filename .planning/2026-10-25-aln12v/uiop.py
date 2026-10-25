#!/usr/bin/env python
"""真机 UI 操作助手：dump → 按 text/desc 找中心坐标 → 可选点击。"""
import argparse, json, os, subprocess, sys
from lxml import etree

ADB = ["adb", "-s", "5748d823"]
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ui_dump.xml")

def dump():
    subprocess.run(ADB + ["shell", "uiautomator", "dump", "/sdcard/_ui.xml"], capture_output=True)
    subprocess.run(ADB + ["pull", "/sdcard/_ui.xml", TMP], capture_output=True, check=True)
    return etree.parse(TMP)

def center(bounds):
    left, top, right, bottom = [int(v) for v in bounds.replace("][", ",").strip("[]").split(",")]
    return (left + right) // 2, (top + bottom) // 2

def nodes(tree):
    for node in tree.iter("node"):
        yield node

def find(tree, *, desc=None, text=None, cls=None, hint=None, min_y=0):
    for node in nodes(tree):
        if cls and node.get("class") != cls:
            continue
        if desc is not None and (node.get("content-desc") or "").strip() != desc:
            continue
        if hint is not None and hint not in (node.get("hint") or ""):
            continue
        if text is not None and text not in (node.get("text") or ""):
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desc"); ap.add_argument("--text"); ap.add_argument("--class", dest="cls_name")
    ap.add_argument("--hint"); ap.add_argument("--min-y", type=int, default=0)
    ap.add_argument("--tap", action="store_true")
    args = ap.parse_args()
    tree = dump()
    node, point = find(tree, desc=args.desc, text=args.text, cls=args.cls_name, hint=args.hint, min_y=args.min_y)
    if node is None:
        print(json.dumps({"found": False})); return 1
    info = {"found": True, "class": node.get("class"), "text": node.get("text"),
            "desc": node.get("content-desc"), "bounds": node.get("bounds"),
            "enabled": node.get("enabled"), "point": point}
    if args.tap:
        subprocess.run(ADB + ["shell", "input", "tap", str(point[0]), str(point[1])], check=True)
        info["tapped"] = True
    print(json.dumps(info, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
