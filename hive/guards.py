"""Guards, version 1: traces of failure counted per session as events over opportunities.

Plan section 3, item 2. Every guard here reads one log file in record order. Only tool calls of
counted messages count (a message id repeated from an earlier file counts in the earliest file), and
each tool_use id counts once. The `interrupt` guard comes from the prompt counter (hive.prompts) and
`notes` from the user's notes; the sensor joins them.

- tool_error: tool results with is_error true / tool results.
- reread: Read calls on a path already read in the file / Read calls.
- reread_after_compact: Read calls on a path read before the last compaction and not after it /
  Read calls after a compaction.
- repeat_command: shell calls (Bash, PowerShell) whose command text equals one of the previous 20
  shell calls / shell calls.
"""
from collections import deque

from .breakdown import read_records

GUARDS_VERSION = "1"
NAMES = ("tool_error", "interrupt", "reread", "reread_after_compact", "repeat_command")
SHELL = ("Bash", "PowerShell")
REPEAT_WINDOW = 20
USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def norm_path(path, fold_case):
    p = path.replace("\\", "/")
    return p.lower() if fold_case else p


def file_guards(path, counted_mids, fold_case):
    """({guard: [events, opportunities]}, format facts) for one log file.

    counted_mids: message ids this file counts. fold_case: compare Read paths without case (Windows).
    Format facts: message ids with usage, and how many of them lack each usage field."""
    g = {name: [0, 0] for name in NAMES if name != "interrupt"}
    seen_uuid, seen_tid = set(), set()
    counted_tids = set()
    all_read, before_last, since_last = set(), set(), set()
    compactions = 0
    shell = deque(maxlen=REPEAT_WINDOW)
    usage_ids = {}
    for obj in read_records(path):
        if obj is None:
            continue
        t = obj.get("type")
        uid = obj.get("uuid")
        if t in ("user", "assistant", "system"):
            if isinstance(uid, str):
                if uid in seen_uuid:
                    continue
                seen_uuid.add(uid)
        if t == "system" and obj.get("subtype") == "compact_boundary":
            compactions += 1
            before_last |= since_last
            since_last = set()
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if t == "assistant":
            if msg.get("model") == "<synthetic>":
                continue
            mid = msg.get("id") or uid
            usage = msg.get("usage")
            if isinstance(usage, dict):
                row = usage_ids.setdefault(mid, set())
                for f in USAGE_FIELDS:
                    v = usage.get(f)
                    if isinstance(v, int) and not isinstance(v, bool):
                        row.add(f)
            if mid not in counted_mids:
                continue
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                tid, name, inp = b.get("id"), b.get("name"), b.get("input")
                if not isinstance(tid, str) or tid in seen_tid:
                    continue
                seen_tid.add(tid)
                counted_tids.add(tid)
                inp = inp if isinstance(inp, dict) else {}
                if name == "Read" and isinstance(inp.get("file_path"), str):
                    p = norm_path(inp["file_path"], fold_case)
                    g["reread"][1] += 1
                    if p in all_read:
                        g["reread"][0] += 1
                    all_read.add(p)
                    if compactions:
                        g["reread_after_compact"][1] += 1
                        if p in before_last and p not in since_last:
                            g["reread_after_compact"][0] += 1
                    since_last.add(p)
                elif name in SHELL:
                    cmd = inp.get("command") if isinstance(inp.get("command"), str) else ""
                    g["repeat_command"][1] += 1
                    if cmd in shell:
                        g["repeat_command"][0] += 1
                    shell.append(cmd)
        elif t == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid in counted_tids:
                        counted_tids.discard(tid)       # a result counts once
                        g["tool_error"][1] += 1
                        if b.get("is_error") is True:
                            g["tool_error"][0] += 1
    facts = {"ids": len(usage_ids),
             "missing": {f: sum(1 for s in usage_ids.values() if f not in s) for f in USAGE_FIELDS}}
    return g, facts


def add(total, part):
    for name, (e, o) in part.items():
        row = total.setdefault(name, [0, 0])
        row[0] += e
        row[1] += o
    return total
