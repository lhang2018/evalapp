#!/usr/bin/env python
"""真机取证辅助：dump UI → 提取可点元素文本与坐标。用法: python uidump.py <name>"""
import re
import subprocess
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
NAME = sys.argv[1] if len(sys.argv) > 1 else "ui"
DIR = "E:/evalapp/.planning/2026-10-24-aln11w-device"
subprocess.run(["adb", "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
               capture_output=True)
subprocess.run(["adb", "pull", "//sdcard/ui.xml", f"{DIR}/{NAME}.xml"],
               capture_output=True)
subprocess.run(["adb", "shell", "screencap", "-p", "/sdcard/screen.png"],
               capture_output=True)
subprocess.run(["adb", "pull", "//sdcard/screen.png", f"{DIR}/{NAME}.png"],
               capture_output=True)
xml = open(f"{DIR}/{NAME}.xml", encoding="utf-8").read()
for m in re.finditer(
        r'text="([^"]{1,80})"[^>]*?bounds="(\[\d+,\d+\]\[\d+,\d+\])"', xml):
    t = m.group(1).strip()
    if t:
        print(f"{t}  @ {m.group(2)}")
