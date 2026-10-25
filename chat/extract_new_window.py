# -*- coding: utf-8 -*-
"""从已捕获的 Local Overrides 原始数据（chat/source*）提取对话归档。

不重新请求；只解析磁盘上已有的 messages 分页 JSON。
输出：chat/extracted/NNN_qa.md（编号续接既有提取，格式与 fetch_chat_full.py 一致：
  # 问答 NN / ## 用户 / ## ChatGPT），并更新 00_目录.md 的新增段落。
用法: python -X utf8 chat/extract_new_window.py [--start N]
"""
import glob
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "extracted")
SOURCES = {
    "旧": os.path.join(BASE, "source", "chatgpt.com", "backend-api",
                       "conversations", "6ad73862*"),
    "新": os.path.join(BASE, "source1", "chatgpt.com", "backend-api",
                       "conversations", "6ad73862*"),
}
CONV_TITLE = "分支 · 分支 · 分支 · 设计自进化智能体"
CONV_ID = "6ad73862-bbcc-83ee-9c54-27de05b3cf6c"


def parts_to_text(m):
    c = m.get("content") or {}
    out = []
    for p in (c.get("parts") or []):
        if isinstance(p, str):
            out.append(p)
        else:
            try:
                out.append("```json\n" + json.dumps(p, ensure_ascii=False,
                                                    indent=1) + "\n```")
            except Exception:
                out.append(str(p))
    t = "\n".join(out).strip()
    if not t and c.get("content_type") == "thoughts":
        t = "\n\n".join(x.get("summary", "") for x in (c.get("thoughts") or [])
                        if isinstance(x, dict))
    return t.strip()


def load_all():
    """按页重建时序：页间按页内最早消息升序拼接（页间已验证不重叠），
    页内保持抓取顺序，最后按 create_time 稳定排序（同秒保留抓取顺序，
    避免批量时间戳打乱问答配对）。"""
    pages = []
    seen = set()
    for tag, pat in SOURCES.items():
        for f in glob.glob(os.path.join(pat, "messages*")):
            try:
                data = json.load(io.open(f, encoding="utf-8"))
            except Exception:
                continue
            page = []
            for m in data.get("messages", []):
                mid = m.get("id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                m["_src"] = tag
                page.append(m)
            if page:
                pages.append(page)
    pages.sort(key=lambda pg: min((m.get("create_time") or 0)
                                  for m in pg))
    ordered = [m for pg in pages for m in pg]
    ordered.sort(key=lambda m: m.get("create_time") or 0)
    return ordered


def existing_max():
    mx = 0
    for f in glob.glob(os.path.join(OUT_DIR, "[0-9]" * 2 + "_*.md")) + \
             glob.glob(os.path.join(OUT_DIR, "[0-9]" * 3 + "_*.md")):
        m = re.match(r"(\d+)", os.path.basename(f))
        if m:
            mx = max(mx, int(m.group(1)))
    return mx


def norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def existing_keys():
    """既有提取的去重键：01~113 各文件「## 用户」段落的 norm 前 80 字。"""
    keys = set()
    for f in glob.glob(os.path.join(OUT_DIR, "[0-9]" * 2 + "_*.md")) + \
            glob.glob(os.path.join(OUT_DIR, "[0-9]" * 3 + "_*.md")):
        try:
            text = io.open(f, encoding="utf-8").read()
        except Exception:
            continue
        m = re.search(r"## 用户\s*\n+(.*?)\n+## ChatGPT", text, re.S)
        if m:
            keys.add(norm(m.group(1))[:80])
    return keys


def main():
    start = None
    if "--start" in sys.argv:
        start = int(sys.argv[sys.argv.index("--start") + 1])
    msgs = load_all()
    # 原始提取的 消息ID→文件 映射（旧窗口已归档部分）
    turns_map = {}
    tmap = os.path.join(OUT_DIR, "_turns.json")
    if os.path.isfile(tmap):
        turns_map = json.load(io.open(tmap, encoding="utf-8"))
    # 归档边界：旧窗口（<2026-10-24 00:00 UTC）已由 01~113 归档；
    # 本脚本只归档新窗口回合。消息级去重已在 load_all 完成。
    cutoff = 1792713600.0  # 2026-10-24 00:00:00 UTC
    users = [m for m in msgs
             if m["author"]["role"] == "user" and parts_to_text(m)
             and m["id"] not in turns_map
             and (m.get("create_time") or 0) >= cutoff]
    print(f"messages={len(msgs)} new_window_user_turns={len(users)}")
    nxt = start if start else existing_max() + 1
    print("next number:", nxt)
    made = []
    idx = []
    for ui, um in enumerate(users):
        ts = um.get("create_time") or 0
        u_text = parts_to_text(um)
        answers = []
        # 按该用户消息在全量序列中的真实位置向后收集，直到下一个用户回合
        pos = next(j for j, m in enumerate(msgs) if m["id"] == um["id"])
        for m in msgs[pos + 1:]:
            if m["author"]["role"] == "user":
                break
            if m["author"]["role"] == "assistant" and parts_to_text(m):
                answers.append(m)
        first = datetime_floor(ts)
        head = u_text.split("\n")[0][:30]
        lines = [f"# 问答 {nxt}", "",
                 f"> 来源: {um.get('_src')} 窗口 | 会话: {CONV_TITLE}",
                 f"> 消息: {um.get('id')} | 时间(UTC): {first}", ""]
        lines += ["## 用户", "", u_text, "", "## ChatGPT", ""]
        for am in answers:
            lines += [parts_to_text(am), ""]
        fn = os.path.join(OUT_DIR, f"{nxt}_qa.md")
        io.open(fn, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
        made.append(fn)
        idx.append((nxt, first, head, len(answers), um.get("_src")))
        nxt += 1
    print("written:", len(made), "files (", made[0], "…", made[-1], ")"
          if made else "")
    update_catalog(idx)
    return 0


def datetime_floor(ts):
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime(
        "%Y-%m-%d %H:%M") if ts else ""


def update_catalog(idx):
    if not idx:
        return
    cat = os.path.join(OUT_DIR, "00_目录.md")
    text = io.open(cat, encoding="utf-8").read() if os.path.isfile(cat) else ""
    marker = "## 2026-10-25 新窗口提取（114+）"
    if marker in text:
        text = text[:text.index(marker)]
    lines = ["", marker, "",
             "> 来源: chat/source1（会话 6ad73862 续篇，2026-10-24 ~ 10-25）；"
             "由 extract_new_window.py 从原始分页提取，编号续接。",
             ""]
    for n, ts, head, na, src in idx:
        lines.append(f"| {n} | {ts} | 回复段:{na} | {head[:50]} |")
    io.open(cat, "w", encoding="utf-8", newline="\n").write(
        text + "\n".join(lines) + "\n")
    print("catalog updated:", cat)


if __name__ == "__main__":
    sys.exit(main())
