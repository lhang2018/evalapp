import json, pathlib, os
path = pathlib.Path(r"E:\evalapp\.planning\2026-10-25-aln12v\device-matrix-evidence.json")
data = json.loads(path.read_text(encoding="utf-8"))
data["app"]["js_source"] = ("Metro dev server 8081 serving working tree; bundle built 2026-10-25T18:08+08:00 "
                            "when repo HEAD was 61451f3 (ALN-29-D docs); working tree also contained other "
                            "authors' uncommitted changes at build time")
data["app"]["js_build_head"] = "61451f3"
data["collected_commit"] = "a40ef427d82e92f22f0cfeadcdb6741a1091fb7c"
path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
print("evidence updated; artifacts", len(data["artifacts"]))
keep = {"device-matrix-evidence.json", "prereboot-state.json", "reboot-compare.json",
        "RKStorage-prereboot.db", "RKStorage-postboot.db", "submit_task.py", "uiop.py",
        "ui_nodes.py", "parse_offline.py", "compare_reboot.py", "build_evidence.py",
        "db_peek.py", "db_peek2.py", "db_keys.py", "api_tasks.py", "api_find_task.py",
        "api_find2.py", "api_after_reboot.py", "db_check.py", "db_check2.py"}
total = 0
for name in sorted(os.listdir(r"E:\evalapp\.planning\2026-10-25-aln12v")):
    ok = (name in keep or name.startswith("ui-") and name.endswith(".xml")
          or name.endswith(".png") and name in {"online2.png", "online-success.png",
              "offline-queued.png", "offline-submitted.png", "after-sync.png",
              "postboot.png", "app3.png"})
    if ok:
        total += os.path.getsize(os.path.join(r"E:\evalapp\.planning\2026-10-25-aln12v", name))
print("kept bytes", total)
