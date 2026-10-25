"""Read local captured messages; audit final-answer pairing without changing drafts."""
from collections import defaultdict
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent
review = root / "outputs/readme-design-review-06d5f7e"
final_dir = review / "finals"
final_dir.mkdir(parents=True, exist_ok=True)
messages, provenance, source_files, variants = {}, defaultdict(list), [], []
archive_source = root / "chat/原稿归档/2026-10-25/source1"
source_base = archive_source if archive_source.is_dir() else root / "chat/source1"


def text(message):
    content = message.get("content") or {}
    if content.get("content_type") != "text":
        return ""
    return "\n".join(p for p in content.get("parts", []) if isinstance(p, str)).strip()


for path in sorted(source_base.rglob("messages*")):
    if not path.is_file():
        continue
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        continue
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        continue
    relative = path.relative_to(root).as_posix()
    source_files.append(dict(path=relative, sha256=hashlib.sha256(raw).hexdigest(), records=len(data["messages"])))
    for message in data["messages"]:
        identity = message.get("id")
        if not identity:
            continue
        if identity in messages and text(messages[identity]) != text(message):
            variants.append(identity)
        messages.setdefault(identity, message)
        provenance[identity].append(relative)

users = sorted([m for m in messages.values() if m.get("author", {}).get("role") == "user" and text(m)],
               key=lambda m: (m.get("create_time") or 0, m["id"]))
finals = sorted([m for m in messages.values() if m.get("author", {}).get("role") == "assistant"
    and m.get("channel") == "final" and m.get("recipient") == "all" and text(m)],
    key=lambda m: (m.get("create_time") or 0, m["id"]))
by_exchange = defaultdict(list)
for message in finals:
    by_exchange[message.get("metadata", {}).get("turn_exchange_id")].append(message)
    (final_dir / (message["id"] + ".md")).write_text(text(message) + "\n", encoding="utf-8", newline="\n")

turns = []
for number, user in enumerate(users, 114):
    exchange = user.get("metadata", {}).get("turn_exchange_id")
    answers = by_exchange.get(exchange, []) if exchange else []
    draft = root / f"chat/extracted/{number}_qa.md"
    draft_raw = draft.read_bytes() if draft.is_file() else None
    draft_text = draft_raw.decode("utf-8") if draft_raw is not None else ""
    draft_body = draft_text.partition("## ChatGPT")[2].strip()
    bound_user = re.search(r"> 消息:\s*([^\s|]+)", draft_text)
    # Existing numbering is used only as an audit label, never as a pairing key.
    turns.append(dict(qa=number, user_id=user["id"], exchange_id=exchange,
        utc=datetime.datetime.fromtimestamp(user["create_time"], datetime.timezone.utc).isoformat(),
        user_request=text(user), final_ids=[m["id"] for m in answers],
        final_sha256=[hashlib.sha256(text(m).encode()).hexdigest() for m in answers],
        draft_path=draft.relative_to(root).as_posix(),
        draft_sha256=hashlib.sha256(draft_raw).hexdigest() if draft_raw is not None else None,
        draft_user_id_matches=bool(bound_user and bound_user.group(1) == user["id"]),
        draft_matches_final=len(answers) == 1 and draft_body == text(answers[0]),
        source_paths=provenance[user["id"]]))

groups = defaultdict(list)
for turn in turns:
    groups[turn["exchange_id"]].append(turn["qa"])
result = dict(scope="read-only captured-message audit; prefer tracked archive; final text only; original source preserved",
    head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    source_files=source_files, unique_messages=len(messages), user_messages=len(users),
    user_exchanges=len(groups), final_messages=len(finals),
    users_with_exactly_one_final=sum(len(t["final_ids"]) == 1 for t in turns),
    users_without_final=[t["qa"] for t in turns if not t["final_ids"]],
    shared_exchange_groups=[v for v in groups.values() if len(v) > 1],
    content_variant_ids=sorted(set(variants)),
    drafts_matching_final=sum(t["draft_matches_final"] for t in turns),
    turns=turns,
    finals=[dict(id=m["id"], exchange_id=m.get("metadata", {}).get("turn_exchange_id"),
        utc=datetime.datetime.fromtimestamp(m["create_time"], datetime.timezone.utc).isoformat(),
        sha256=hashlib.sha256(text(m).encode()).hexdigest(), chars=len(text(m)),
        headings=[line for line in text(m).splitlines() if line.startswith("#")], source_paths=provenance[m["id"]]) for m in finals])
(out / "corpus-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
classes = defaultdict(int)
for turn in turns:
    body = (root / turn["draft_path"]).read_text(encoding="utf-8").partition("## ChatGPT")[2].strip()
    if not turn["final_ids"]:
        category = "no_final_in_source"
    else:
        answer = text(messages[turn["final_ids"][0]])
        category = ("exact" if body == answer else
                    "contains_final_with_extra" if answer in body else
                    "not_containing_corresponding_final")
    classes[category] += 1
(out / "draft-classification.json").write_text(
    json.dumps(dict(classes=dict(classes)), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
print(json.dumps({k: result[k] for k in ("unique_messages", "user_messages", "user_exchanges", "final_messages",
    "users_with_exactly_one_final", "users_without_final", "shared_exchange_groups", "content_variant_ids", "drafts_matching_final")},
    ensure_ascii=False, indent=2))
