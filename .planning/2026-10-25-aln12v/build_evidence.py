import hashlib, json, os, subprocess, time
from lxml import etree

BASE = r"E:\evalapp\.planning\2026-10-25-aln12v"
def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()

def ui_summary(name):
    tree = etree.parse(os.path.join(BASE, name))
    texts = [(n.get("text") or "").strip() for n in tree.iter("node")]
    queue = [t for t in texts if t.startswith("待上传")]
    banner = [t for t in texts if "在线" in t or "离线" in t][:2]
    results = [t for t in texts if t.startswith("结果:")][:4]
    return {"queue_line": queue[0] if queue else "", "banner": banner,
            "results": results}

def adb(*args, timeout=60):
    return subprocess.run(["adb", "-s", "5748d823", *args], capture_output=True, text=True, timeout=timeout).stdout

def adb_bin(*args, timeout=60):
    return subprocess.run(["adb", "-s", "5748d823", *args], capture_output=True, timeout=timeout).stdout

evidence = {
    "task": "ALN-12-V 通知、离线与系统重启真机矩阵",
    "collected_at": time.time(),
    "device": {
        "serial": "5748d823", "model": adb("shell", "getprop", "ro.product.model").strip(),
        "android": adb("shell", "getprop", "ro.build.version.release").strip(),
        "build": adb("shell", "getprop", "ro.build.display.id").strip(),
    },
    "app": {
        "package": "com.evoagent",
        "version_name": "0.1.0", "version_code": 1,
        "apk_sha256": "7b72b7997e842ba806841352dfc2e1494617a73f4dacbeb7d30ee85599fdeda2",
        "js_bundle_sha256": "a0eb06f5af793b0590bab6937785483b61c46c61ae8a67abf3494cccd57b4e9f",
        "js_bundle_bytes": 6558625,
        "js_source": "Metro dev server 8081 from working tree at commit "
                      + subprocess.run(["git", "-C", r"E:\evalapp", "rev-parse", "HEAD"],
                                       capture_output=True, text=True).stdout.strip(),
        "accessibility_service": "com.evoagent/.DeviceAccessibilityService (enabled)",
        "post_notification_permission": "allow",
    },
    "notification_probe": {
        "attempted": True,
        "method": "离线排队任务→后台 WorkManager SyncWorker（headless JS）→恢复 adb reverse 8300→"
                  "cmd jobscheduler run -f com.evoagent 58",
        "result": "not_observed",
        "details": {
            "evoagent_notifications_after": 0,
            "background_sync_evidence": "后台任务后后端出现新任务 b4c98a2c95a0(calculate) 与 "
                                        "3ee9cd585c67(calculate)，说明 headless 同步执行成功",
            "limitation": "未在 dumpsys notification 中观测到 com.evoagent 通知；"
                          "logcat 中也无 [background-sync] 行（缓冲/时序），"
                          "不宣称通知已投递，按未验证记录",
        },
    },
    "online_baseline": {
        "ui": ui_summary("ui-online2.xml"),
        "backend_task": {"task_id": "3e1cf341048b", "status": "SUCCEEDED",
                         "goal": "calculate 1+1"},
    },
    "offline_queue": {
        "ui_queued": ui_summary("ui-offline-submitted.xml"),
        "queued_line": "待上传 1 · 容量 1/500",
        "backend_before_replay": "无新任务（latest=3e1cf341048b）",
        "persisted_state_key": "offline_state_v1",
        "persisted_state_digest": json.load(open(os.path.join(BASE, "prereboot-state.json"),
                                                 encoding="utf-8"))["state_digest"],
        "local_tasks_persisted": 14,
    },
    "replay_on_reconnect": {
        "ui_after_sync": ui_summary("ui-after-sync.xml"),
        "result_line": "结果: 计算结果：11",
        "queue_after": "待上传 0 · checkpoint 22→25",
        "backend_new_task": {"task_id": "a3ee7b879193", "status": "SUCCEEDED",
                             "goal": "calculate 5+6"},
    },
    "reboot_recovery": json.load(open(os.path.join(BASE, "reboot-compare.json"),
                                      encoding="utf-8"))["comparison"],
    "backend_after_reboot": {"task_count": 13,
                             "calculate_tasks": ["b4c98a2c95a0", "3ee9cd585c67"]},
    "artifacts": {},
}
for name in sorted(os.listdir(BASE)):
    if name.endswith((".png", ".xml", ".json", ".log", ".db", ".bundle.js", ".py", ".txt")):
        path = os.path.join(BASE, name)
        evidence["artifacts"][name] = {"sha256": sha(path), "bytes": os.path.getsize(path)}
open(os.path.join(BASE, "device-matrix-evidence.json"), "w", encoding="utf-8").write(
    json.dumps(evidence, ensure_ascii=False, indent=1))
print(json.dumps({k: evidence[k] for k in ("device", "app", "notification_probe",
                                           "reboot_recovery")}, ensure_ascii=False, indent=1)[:2000])
print("artifacts:", len(evidence["artifacts"]))
