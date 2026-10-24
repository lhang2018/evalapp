"""Capture the authorized test app; print compact visible text only."""
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8")
out = Path(__file__).resolve().parent
name = sys.argv[1]
assert name.replace('-', '').replace('_', '').isalnum()


def adb(*args):
    return subprocess.check_output(["adb", "-s", "5748d823", *args])


adb("shell", "uiautomator", "dump", "/sdcard/review-ui.xml")
xml = adb("exec-out", "cat", "/sdcard/review-ui.xml")
(out / (name + ".xml")).write_bytes(xml)
(out / (name + ".png")).write_bytes(adb("exec-out", "screencap", "-p"))
nodes = ET.fromstring(xml).iter("node")
print(json.dumps([dict(text=n.get("text"), bounds=n.get("bounds"), enabled=n.get("enabled"))
    for n in nodes if n.get("text")], ensure_ascii=False)[:7500])
