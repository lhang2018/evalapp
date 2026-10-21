# -*- coding: utf-8 -*-
"""ChatGPT 对话全量导出脚本（自包含，一键完成）。

用法（在 E:\\evalapp 下）:
  1. 把 DevTools 里 Copy as cURL 的内容原样粘贴保存为 chat/curl.txt（cmd 的 ^ 转义可自动处理）
  2. 运行:  python -X utf8 chat/fetch_chat_full.py
  3. 产出:
     - chat/raw/chatgpt_conversation_<id>.json   原始全量 JSON（含所有分支）
     - chat/extracted/NN_qa.md                   每问答一个 md（与已有文件去重、编号续接）
     - chat/extracted/branches/v*.md             不在当前路径的分支/重新生成变体
     - chat/extracted/00_目录.md                 重新生成的目录与完整性报告

请求全部串行 + 间隔，收到 429/5xx 自动退避重试，避免限流。
凭据只在本脚本内存中使用；用完可自行删除 chat/curl.txt。
"""
import glob
import io
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))          # chat/
REPO = os.path.dirname(BASE)                                # E:\evalapp
RAW_DIR = os.path.join(BASE, "raw")
OUT_DIR = os.path.join(BASE, "extracted")
BR_DIR = os.path.join(OUT_DIR, "branches")

# 目标会话标题关键词（命中任一即抓取；改成 [] 且 ALL=True 则抓全部）
TARGET_KEYWORDS = ["自进化", "智能体", "EvoAgent", "evoagent"]
FETCH_ALL = False
LIMIT_PAGES = 5          # 每页 100 个会话，最多翻 5 页
SLEEP = 2.0              # 请求间隔（秒）
RETRIES = 3              # 429/5xx 重试次数
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- curl 解析
def unescape_cmd(t: str) -> str:
    out, i = [], 0
    while i < len(t):
        c = t[i]
        if c == "^" and i + 1 < len(t):
            out.append(t[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def read_curl_file():
    cands = sorted(glob.glob(os.path.join(BASE, "*.txt")))
    for p in cands:
        t = io.open(p, encoding="utf-8", errors="replace").read()
        if "authorization:" in t.lower() and "bearer" in t.lower():
            log(f"curl 文件: {os.path.basename(p)}")
            return t
    log("!! chat/ 下没找到含 'authorization: Bearer' 的 .txt —— 请把 curl 原样存为 chat/curl.txt")
    sys.exit(1)


def extract_creds(text: str):
    flat = unescape_cmd(re.sub(r"(\^\r?\n|\r?\n)", " ", text))
    m = re.search(r"authorization:\s*[Bb]earer\s+([A-Za-z0-9_\-.]+)", flat)
    bearer = m.group(1) if m else None
    m = (re.search(r'-b\s+"([^"]+)"', flat)
         or re.search(r"--cookie\s+\"([^\"]+)\"", flat)
         or re.search(r"-b\s+'([^']+)'", flat))
    cookie = m.group(1) if m else None
    m = re.search(r"chatgpt-account-id:\s*([0-9a-f-]{36})", flat, re.I)
    acct = m.group(1) if m else None
    m = re.search(r"oai-did:\s*([0-9a-f-]{36})", flat, re.I)
    did = m.group(1) if m else None
    return bearer, cookie, acct, did


# ---------------------------------------------------------------- 串行请求
class Client:
    def __init__(self, bearer, cookie, acct, did):
        self.bearer, self.cookie, self.acct, self.did = bearer, cookie, acct, did
        self.calls = 0

    def get(self, url, out_path):
        for attempt in range(1, RETRIES + 1):
            if self.calls:
                time.sleep(SLEEP)
            self.calls += 1
            cmd = ["curl.exe", "-s", "-S", "--compressed",
                   "-x", "http://127.0.0.1:7897",
                   "--url", url,
                   "-H", f"authorization: Bearer {self.bearer}",
                   "-H", "accept: */*",
                   "-H", "accept-language: zh-CN,zh;q=0.9",
                   "-H", "oai-language: zh-CN",
                   "-H", "originator: Codex Browser",
                   "-H", "referer: https://chatgpt.com/",
                   "-H", f"user-agent: {UA}"]
            if self.acct:
                cmd += ["-H", f"chatgpt-account-id: {self.acct}"]
            if self.did:
                cmd += ["-H", f"oai-did: {self.did}"]
            if self.cookie:
                cmd += ["-b", self.cookie]
            cmd += ["-o", out_path, "-w", "%{http_code}"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            code = (r.stdout or "").strip()
            if code == "200":
                return 200
            if (r.stderr or "").strip():
                log("   curl stderr:", (r.stderr or "").strip()[:400])
            log(f"   HTTP {code} (第 {attempt} 次) {url[:80]}")
            if code in ("429", "500", "502", "503", "504"):
                wait = 10 * attempt
                log(f"   限流/服务端错误，退避 {wait}s ...")
                time.sleep(wait)
                continue
            break
        return int(code) if code.isdigit() else 0


# ---------------------------------------------------------------- 解析
def norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


def parts_to_text(m):
    c = m.get("content") or {}
    out = []
    for p in (c.get("parts") or []):
        if isinstance(p, str):
            out.append(p)
        else:
            try:
                out.append("```json\n" + json.dumps(p, ensure_ascii=False, indent=1) + "\n```")
            except Exception:
                out.append(str(p))
    t = "\n".join(out).strip()
    if not t and c.get("content_type") == "thoughts":
        t = "\n\n".join(x.get("summary", "") for x in (c.get("thoughts") or [])
                        if isinstance(x, dict))
    return t.strip()


def walk_active_path(j):
    path, seen = [], set()
    cur = j.get("current_node")
    mapping = j.get("mapping") or {}
    while cur and cur in mapping and cur not in seen:
        seen.add(cur)
        n = mapping[cur]
        if n.get("message"):
            path.append((cur, n["message"]))
        cur = n.get("parent")
    path.reverse()
    return path


def pairs_from_path(path):
    pairs, cur_user, cur_ans = [], None, []
    for nid, m in path:
        role = ((m.get("author") or {}).get("role")) or ""
        text = parts_to_text(m)
        if not text:
            continue
        if role == "user":
            if cur_user or cur_ans:
                pairs.append((cur_user, cur_ans))
            cur_user, cur_ans = (nid, m), []
        elif role == "assistant" and cur_user is not None:
            cur_ans.append((nid, m))
    if cur_user or cur_ans:
        pairs.append((cur_user, cur_ans))
    return pairs


def existing_extracted():
    """{norm_key: (序号, 文件路径)}，用于去重与编号续接"""
    out = {}
    for p in sorted(glob.glob(os.path.join(OUT_DIR, "[0-9][0-9]_*.md"))):
        t = io.open(p, encoding="utf-8", errors="replace").read()
        mu = re.search(r"## 用户\s*\n\n(.*?)\n\n## ChatGPT", t, re.S)
        mnum = re.match(r"(\d+)", os.path.basename(p))
        if mu:
            out[norm(mu.group(1))[:80]] = (int(mnum.group(1)) if mnum else 0, p)
    return out


def parse_conversation(j, src_name):
    title = j.get("title") or "对话"
    mapping = j.get("mapping") or {}
    path = walk_active_path(j)
    pairs = pairs_from_path(path)
    if not pairs and len(mapping) > 3:
        # 结构与预期不符：打印一个非空消息节点样例便于适配
        for nid, n in mapping.items():
            m = n.get("message")
            if m and ((m.get("content") or {}).get("parts") is not None):
                log("   !! 问答组为 0 但节点很多 —— 消息节点样例:")
                log("   " + json.dumps(m, ensure_ascii=False)[:600])
                break
    active_ids = {nid for nid, _ in path}

    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(BR_DIR, exist_ok=True)
    exist = existing_extracted()
    nxt = max([v[0] for v in exist.values()], default=0) + 1

    made, skipped = [], 0
    for (uid, um), ans in pairs:
        u_text = parts_to_text(um)
        key = norm(u_text)[:80]
        if key in exist:
            skipped += 1
            continue
        exist[key] = (nxt, None)
        lines = [f"# 问答 {nxt:02d}", "",
                 f"> 来源: {src_name} | 会话: {title}", ""]
        lines += ["## 用户", "", u_text, "", "## ChatGPT", ""]
        for _aid, am in ans:
            lines += [parts_to_text(am), ""]
        fn = os.path.join(OUT_DIR, f"{nxt:02d}_qa.md")
        io.open(fn, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
        made.append(fn)
        nxt += 1

    # 分支变体
    variants = []
    for nid, n in mapping.items():
        if nid in active_ids:
            continue
        m = n.get("message")
        if not m:
            continue
        role = ((m.get("author") or {}).get("role")) or ""
        if role in ("user", "assistant") and parts_to_text(m):
            variants.append((m.get("create_time") or 0, nid, role, m))
    variants.sort(key=lambda x: x[0])
    vn = len(glob.glob(os.path.join(BR_DIR, "v*.md"))) + 1
    for ct, nid, role, m in variants:
        fn = os.path.join(BR_DIR, f"v{vn:02d}_{role}_{nid[:8]}.md")
        io.open(fn, "w", encoding="utf-8", newline="\n").write(
            f"# 分支变体 {vn:02d}（{role}，不在当前路径；来源 {src_name}）\n\n"
            f"{parts_to_text(m)}\n")
        vn += 1
    return title, len(mapping), len(pairs), made, skipped, len(variants)


def write_catalog(results):
    files = sorted(glob.glob(os.path.join(OUT_DIR, "[0-9][0-9]_*.md")))
    lines = ["# 对话提取目录", "",
             f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M')} | 共 {len(files)} 组问答", "",
             "| # | 文件 | 用户提问首行 |", "|---|---|---|"]
    for p in files:
        t = io.open(p, encoding="utf-8", errors="replace").read()
        mu = re.search(r"## 用户\s*\n\n(.*?)\n", t, re.S)
        first = (mu.group(1).strip()[:60] if mu else "(未解析到)")
        lines.append(f"| {os.path.basename(p)[:2]} | {os.path.basename(p)} | {first} |")
    lines += ["", "## 完整性", ""]
    for (name, nodes, pairs, made, skipped, variants) in results:
        lines.append(f"- {name}: 节点 {nodes} | 主路径问答组 {pairs} | 新增 {len(made)} | 跳过重复 {skipped} | 分支变体 {variants}")
    io.open(os.path.join(OUT_DIR, "00_目录.md"), "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- main
def main():
    text = read_curl_file()
    bearer, cookie, acct, did = extract_creds(text)
    log(f"凭据: Bearer {len(bearer or '')} 字符 | Cookie {len(cookie or '')} 字符 | acct {acct} | did {did}")
    if not bearer:
        log("!! 未提取到 Bearer token")
        sys.exit(1)
    cli = Client(bearer, cookie, acct, did)

    os.makedirs(RAW_DIR, exist_ok=True)
    # ① 对话列表（串行翻页：用 total/limit/offset，无 has_more 字段）
    targets = []
    offset, total, page = 0, None, 0
    while offset is not None and page < LIMIT_PAGES:
        url = (f"https://chatgpt.com/backend-api/conversations?limit=100&order=updated"
               f"&offset={offset}&is_archived=false&is_starred=false")
        tmp = os.path.join(RAW_DIR, "_list.json")
        code = cli.get(url, tmp)
        if code != 200:
            log(f"!! 列表接口 HTTP {code} —— 凭据失效(401/403)或被 Cloudflare 拦截；"
                f"可改用浏览器 Console 的 fetch 脚本方案")
            body = io.open(tmp, encoding="utf-8", errors="replace").read()[:300]
            log("   响应样例:", body)
            sys.exit(2)
        data = json.load(io.open(tmp, encoding="utf-8"))
        items = data.get("items") or data.get("conversations") or []
        total = data.get("total", offset + len(items))
        log(f"列表第 {page + 1} 页: {len(items)} 个会话 (offset {offset}/{total})")
        for it in items:
            cid = it.get("id") or it.get("conversation_id") or it.get("uuid")
            ttl = it.get("title") or ""
            if FETCH_ALL or any(k.lower() in ttl.lower() for k in TARGET_KEYWORDS):
                targets.append((it.get("create_time") or "", cid, ttl))
        offset += len(items)
        page += 1
        if offset >= total or not items:
            break
    if not targets:
        log("!! 未命中目标会话；请调大 TARGET_KEYWORDS 或设 FETCH_ALL=True")
        sys.exit(3)
    # 根会话在前（创建时间正序），分支重复前缀靠去重跳过、只收新增
    targets.sort(key=lambda x: x[0])
    log(f"目标会话 {len(targets)} 个（按创建时间正序）:")
    for ct, cid, ttl in targets:
        log(f"   {ct[:19]}  {cid}  {ttl}")

    # ② 逐个拉全量 JSON（串行，根会话先抓）
    results = []
    for _ct, cid, ttl in targets:
        out_json = os.path.join(RAW_DIR, f"chatgpt_conversation_{cid}.json")
        code = cli.get(f"https://chatgpt.com/backend-api/conversation/{cid}", out_json)
        if code != 200:
            log(f"!! {cid} 详情 HTTP {code}，跳过")
            continue
        j = json.load(io.open(out_json, encoding="utf-8"))
        if "mapping" not in j:
            log(f"!! {cid} 返回无 mapping 字段，顶层键: {list(j.keys())[:20]}")
            log("   结构样例(前500字):", json.dumps(j, ensure_ascii=False)[:500])
            log("   → 把这段发给我，我立刻适配结构")
            continue
        r = parse_conversation(j, os.path.basename(out_json))
        results.append((j.get("title") or ttl, *r))
        log(f"   ✅ {j.get('title') or ttl}: 节点 {r[0]} | 问答组 {r[1]} | 新增 {len(r[2])} | 变体 {r[4]}")

    write_catalog(results)
    log(f"\n完成: 共 {sum(len(r[2]) for r in results)} 组新 md；目录已更新 chat/extracted/00_目录.md")
    log("提醒: chat/curl.txt 内含登录凭据，任务完成后建议删除。")


if __name__ == "__main__":
    main()
