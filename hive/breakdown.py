"""Token breakdown of Claude Code session logs: a port of the private research tool token_breakdown.py.

The analysis code below (log reading, steps, item sizes, weights, rows) is copied line for line from
token_breakdown.py of the development repository, which records cite and which stays unchanged.
Differences, none of which changes a number:
- prices come from a weights file (weights/<id>.json) instead of a constant;
- items of tool results carry the tool name and tool_use id (item.tool, item.tid), and the
  analysis keeps the running read weight (an.wr_cum), for the replay screen;
- the probe, the report and the command line are left out.
The parity test of the development repository runs both on the same fixtures.
Standard library only.
"""
import base64
import json
import statistics
import struct
from collections import Counter, defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace

from . import weights as _weights


USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens")

# ---------------------------------------------------------------- log reading

def find_logs(dirs):
    """Every (root, path) for a .jsonl file under the log directories."""
    return [(d, p) for d in dirs for p in sorted(d.rglob("*.jsonl"))]


def is_subagent_path(root, path):
    return "subagents" in path.relative_to(root).parts


def read_records(path):
    """Yield one dict per JSON line, or None for a line that is not a JSON object."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                yield None
                continue
            yield obj if isinstance(obj, dict) else None


def collapse_tool(name):
    """mcp__server__tool becomes mcp:server; other names stay."""
    if isinstance(name, str) and name.startswith("mcp__"):
        parts = name.split("__")
        return "mcp:" + (parts[1] if len(parts) > 1 else "")
    return name if isinstance(name, str) else "<none>"


# ---------------------------------------------------------------- image header

def jpeg_size(raw):
    if raw[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(raw):
        if raw[i] != 0xFF:
            i += 1
            continue
        m = raw[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", raw[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
    return None


def image_size(b64, media_type):
    """(width, height) read from the image header, or None."""
    try:
        if media_type == "image/jpeg":
            return jpeg_size(base64.b64decode(b64 + "=" * (-len(b64) % 4)))
        head = b64[:128]
        raw = base64.b64decode(head[:len(head) // 4 * 4])
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", raw[16:24])
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", raw[6:10])
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            kind = raw[12:16]
            if kind == b"VP8X":
                return (1 + int.from_bytes(raw[24:27], "little"),
                        1 + int.from_bytes(raw[27:30], "little"))
            if kind == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
                return (struct.unpack("<H", raw[26:28])[0] & 0x3FFF,
                        struct.unpack("<H", raw[28:30])[0] & 0x3FFF)
            if kind == b"VP8L" and raw[20] == 0x2F:
                bits = int.from_bytes(raw[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except (ValueError, struct.error, IndexError, TypeError):
        return None
    return None

# -------------------------------------------------------------------- constants

# USD per million tokens by model, from a weights file (hive.weights). use_weights() switches the file.
WEIGHTS = _weights.DEFAULT
PRICES = _weights.prices(WEIGHTS)


def use_weights(weights_id):
    """Price the analysis with another weights file. Changes the module-wide PRICES."""
    global WEIGHTS
    new = _weights.prices(weights_id)       # load first: a bad id leaves the prices as they were
    PRICES.clear()
    PRICES.update(new)
    WEIGHTS = weights_id


def norm_project(text):
    """A working folder as one comparable string: forward slashes, lower case (as session_tokens.py)."""
    return str(text).replace("\\", "/").lower()


IMAGE_DIVISOR = 750.0           # tokens = width * height / 750 (Anthropic's published estimate)
MIN_SINGLE_TOKENS = 300         # single-item steps below this are not used for ratios
FALLBACK_CHARS_PER_TOKEN = 2.39  # median of the plan's prototype; used only when no session has a sample
RESIDUAL_FLAG = 0.02
BIG_REBUILD = 1000              # rebuilds of at least this many tokens are also counted on their own
FIRST_LIKE = ("first", "compaction", "restart")

# Claude Code's own tools. A tool definition that is neither one of these nor an
# MCP tool is grouped as "other tools" in the prefix split (amendment 1, E).
BUILTIN_TOOLS = frozenset({
    "Agent", "Task", "Artifact", "AskUserQuestion", "Bash", "PowerShell", "Edit", "Write",
    "NotebookEdit", "Read", "Glob", "Grep", "Skill", "ToolSearch", "DeferredToolPlaceholder",
    "advisor", "ListAgents", "ReportFindings", "ScheduleWakeup", "SendFeedback", "Workflow",
    "Monitor", "SearchPlugins", "SendUserFile", "SuggestPluginInstall", "SuggestSkills",
    "WebFetch", "WebSearch", "SubagentHandback", "SendMessage", "TaskCreate", "TaskUpdate",
    "TaskGet", "TaskList", "TaskStop", "TaskOutput", "EnterPlanMode", "ExitPlanMode",
    "EnterWorktree", "ExitWorktree", "CronCreate", "CronDelete", "CronList", "LSP",
    "PushNotification", "RemoteTrigger", "ListMcpResourcesTool", "ReadMcpResourceTool",
    "EndConversation", "BashOutput", "KillShell", "TodoWrite",
})


def price_for(model):
    """Price dict for a model id (exact, else longest key that is a prefix), or None."""
    if not isinstance(model, str):
        return None
    if model in PRICES:
        return PRICES[model]
    best = None
    for key in PRICES:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return PRICES[best] if best else None


def cjson(obj):
    try:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def tool_group(name):
    """Row label of a tool: Read, Grep/Glob, Bash/PowerShell, Edit/Write, web, Agent, mcp:<server>, other."""
    if not isinstance(name, str):
        return "other"
    if name.startswith("mcp__"):
        return collapse_tool(name)
    if name == "Read":
        return "Read"
    if name in ("Grep", "Glob"):
        return "Grep/Glob"
    if name in ("Bash", "PowerShell"):
        return "Bash/PowerShell"
    if name in ("Edit", "Write", "NotebookEdit"):
        return "Edit/Write"
    if name in ("WebFetch", "WebSearch"):
        return "web"
    if name in ("Agent", "Task"):
        return "Agent"
    return "other"


def tool_def_group(name):
    """Prefix split group of a tool definition."""
    if isinstance(name, str) and name.startswith("mcp__"):
        return collapse_tool(name)
    if name in BUILTIN_TOOLS:
        return "built-in tools"
    return "other tools"


def short_ext(path):
    if not isinstance(path, str) or not path:
        return "-"
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else "(none)"


def first_word(cmd):
    if not isinstance(cmd, str):
        return "-"
    parts = cmd.split()
    return parts[0] if parts else "-"


def parse_ts(ts):
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ data model

class Rec:
    """One conversation record (user, assistant, attachment or system)."""
    __slots__ = ("pos", "uuid", "parent", "pp", "ts", "kind", "label", "call", "items",
                 "preserved", "enter")

    def __init__(self, pos, uuid, parent, ts):
        self.pos = pos
        self.uuid = uuid
        self.parent = parent
        self.pp = None          # position of the parent record, resolved after reading
        self.ts = ts
        self.kind = "S"         # A assistant call record, U user, T attachment, B compact boundary, S other
        self.label = "system"
        self.call = -1
        self.items = []
        self.preserved = []
        self.enter = None       # index of the first call that has this record in its context


class Item:
    """Something that takes tokens in the context: a record, or a part of one."""

    def __init__(self, cat, sub, chars=0, recs=(), img=None, out_call=None, meta="-", path=None,
                 size=None):
        self.cat = cat
        self.sub = sub
        self.chars = chars
        self.recs = list(recs)
        self.img = img
        self.out_call = out_call        # call that produced it, for output items
        self.meta = meta                # file extension or first word of a command
        self.path = path                # shown only with --paths
        self.size = size                # tokens; None until sized or estimated
        self.enter = None
        self.segs = []                  # presence as (first call, last call) segments
        self.unsized = False            # entered in a step that was excluded
        self.estimated = False
        self.unsized_refs = 0
        self.split = None               # prefix item: label -> share
        self.new = self.carried = self.out = self.w = 0.0
        self.carried_calls = 0
        self.tool = self.tid = None     # hive: tool name and tool_use id of a tool result
        self.is_error = False           # hive: the tool result has is_error true


class Call:
    def __init__(self, idx, mid, rec, model, ts, version):
        self.idx = idx
        self.mid = mid
        self.pos = rec.pos
        self.model = model
        self.ts = ts
        self.version = version
        self.recs = []
        self.u = {"input": 0, "cw": 0, "cr": 0, "out": 0, "think": 0, "cw5": 0, "cw1": 0,
                  "has_split": False}
        self.req_ref = None
        self.tool_ids = []
        self.think_recs = []
        self.think_item = None
        self.out_items = []
        self.unattached = 0.0
        self.counted = True

    @property
    def context(self):
        return self.u["input"] + self.u["cw"] + self.u["cr"]


class FileData:
    def __init__(self, path, kind):
        self.path = path
        self.kind = kind
        self.sid = path.stem[:8]
        self.project = None
        self.first_ts = None
        self.recs = []
        self.uuid_idx = {}
        self.calls = []
        self.call_by_mid = {}
        self.items = []
        self.shapes = {}                # shape hash -> metrics
        self.req_shape = {}             # api-request id -> shape hash
        self.shape_order = []
        self.defs = {}                  # tool name -> characters of its definition
        self.result_rec = {}            # tool_use id -> position of its tool_result record
        self.tool_info = {}             # tool_use id -> (name, row label, type, path)
        self.thinking_events = []       # (calls before it, attachment type, blocks dropped, turns dropped)
        self.index = 0
        self.date = ""
        self.cost_state = None
        self.synthetic = 0

    def add_item(self, item):
        self.items.append(item)
        for r in item.recs:
            r.items.append(item)
        return item


# ---------------------------------------------------------------------- parsing

def image_dims(block):
    src = block.get("source") if isinstance(block, dict) else None
    if isinstance(src, dict) and isinstance(src.get("data"), str):
        return image_size(src["data"], src.get("media_type"))
    return None


def shape_metrics(shape):
    """Characters of an api-request-shape: system text and tool definitions by group."""
    sys_chars = 0
    for part in shape.get("system") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            sys_chars += len(part["text"])
    groups = Counter()
    defs = {}
    for tool in shape.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        d = {k: tool[k] for k in ("name", "description", "input_schema") if k in tool}
        n = len(cjson(d))
        name = tool.get("name")
        groups[tool_def_group(name)] += n
        if isinstance(name, str):
            defs[name] = n
    tools_chars = sum(groups.values())
    return {"sys": sys_chars, "tools": tools_chars, "total": sys_chars + tools_chars,
            "groups": dict(groups), "defs": defs}


def merge_usage(call, usage):
    u = call.u
    for key, field in (("input", "input_tokens"), ("cw", "cache_creation_input_tokens"),
                       ("cr", "cache_read_input_tokens"), ("out", "output_tokens")):
        v = usage.get(field)
        if isinstance(v, int):
            u[key] = max(u[key], v)
    det = usage.get("output_tokens_details")
    if isinstance(det, dict) and isinstance(det.get("thinking_tokens"), int):
        u["think"] = max(u["think"], det["thinking_tokens"])
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        u["has_split"] = True
        u["cw5"] = max(u["cw5"], cc.get("ephemeral_5m_input_tokens") or 0)
        u["cw1"] = max(u["cw1"], cc.get("ephemeral_1h_input_tokens") or 0)


def parse_assistant(fd, rec, obj, ts):
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return
    if msg.get("model") == "<synthetic>":
        fd.synthetic += 1                  # amendment 1, I: not a call, not an item
        return
    mid = msg.get("id") or obj.get("uuid")
    call = fd.call_by_mid.get(mid)
    if call is None:
        call = Call(len(fd.calls), mid, rec, msg.get("model"), ts, obj.get("version"))
        fd.calls.append(call)
        fd.call_by_mid[mid] = call
    rec.kind, rec.call = "A", call.idx
    call.recs.append(rec)
    if isinstance(msg.get("usage"), dict):
        merge_usage(call, msg["usage"])
    rr = obj.get("requestRef")
    if isinstance(rr, str) and call.req_ref is None:
        call.req_ref = rr
    wire = obj.get("wireToolInputs")
    wire = wire if isinstance(wire, dict) else {}
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt in ("thinking", "redacted_thinking"):
            rec.label = "A:thinking"
            call.think_recs.append(rec)
        elif bt == "text":
            rec.label = "A:text"
            text = b.get("text")
            it = Item("assistant text", "-", len(text) if isinstance(text, str) else 0, [rec],
                      out_call=call.idx)
            call.out_items.append(fd.add_item(it))
        elif bt == "tool_use":
            rec.label = "A:tool_use"
            tid, name = b.get("id"), b.get("name")
            inp = wire.get(tid) if isinstance(wire.get(tid), dict) else b.get("input")
            group = tool_group(name)
            meta, path = "-", None
            if isinstance(inp, dict):
                if group in ("Read", "Edit/Write"):
                    path = inp.get("file_path") if isinstance(inp.get("file_path"), str) else None
                    meta = short_ext(path)
                elif group == "Bash/PowerShell":
                    path = inp.get("command") if isinstance(inp.get("command"), str) else None
                    meta = first_word(path)
                elif group == "Grep/Glob":
                    path = inp.get("path") if isinstance(inp.get("path"), str) else None
            it = Item("tool input", group, len(cjson(inp)), [rec], out_call=call.idx, meta=meta,
                      path=path)
            call.out_items.append(fd.add_item(it))
            if isinstance(tid, str):
                call.tool_ids.append(tid)
                fd.tool_info[tid] = (name, group, meta, path)


def parse_user(fd, rec, obj, pending):
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    rec.kind = "U"
    summary, meta = bool(obj.get("isCompactSummary")), bool(obj.get("isMeta"))
    text_chars, images, has_result = 0, [], False
    if isinstance(content, str):
        text_chars = len(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                t = b.get("text")
                text_chars += len(t) if isinstance(t, str) else 0
            elif bt == "image":
                images.append(image_dims(b))
            elif bt == "tool_result":
                has_result = True
                pending.append((rec, b))
    if summary:
        cat, sub, rec.label = "compaction summary", "-", "U:summary"
    elif meta:
        cat, sub, rec.label = "injected", "isMeta user", "U:meta"
    else:
        cat, sub, rec.label = "prompt", "typed or pasted", ("U:tool_result" if has_result else "U:prompt")
    if text_chars or isinstance(content, str):
        fd.add_item(Item(cat, sub, text_chars, [rec]))
    for dims in images:
        fd.add_item(Item("image", "from prompt", 0, [rec], img=dims))


def parse_attachment(fd, rec, obj):
    att = obj.get("attachment")
    at = att.get("type") if isinstance(att, dict) else None
    at = at if isinstance(at, str) else "<none>"
    rec.kind, rec.label = "T", "T:" + at
    rendered = obj.get("rendered")
    chars = 0
    if isinstance(rendered, list):
        for part in rendered:
            if isinstance(part, dict) and isinstance(part.get("content"), str):
                chars += len(part["content"])
    if isinstance(att, dict) and at in ("thinking_drop", "thinking_stripped"):
        nd = att.get("newlyDropped") if isinstance(att.get("newlyDropped"), dict) else {}
        blocks, turns = nd.get("blockCount"), nd.get("turnCount")
        fd.thinking_events.append((len(fd.calls), at, blocks if isinstance(blocks, int) else None,
                                   turns if isinstance(turns, int) else None))
    if isinstance(att, dict) and at == "deferred_tools_record":
        for e in att.get("entries") or []:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                d = {k: e[k] for k in ("name", "description", "input_schema") if k in e}
                fd.defs.setdefault(e["name"], len(cjson(d)))
    if at == "queued_command":
        if not chars and isinstance(att.get("prompt"), str):
            chars = len(att["prompt"])
        fd.add_item(Item("prompt", "queued while working", chars, [rec]))
    else:
        fd.add_item(Item("injected", at, chars, [rec]))


def parse_file(root, path):
    """Read one log file into records, calls and items (no text is kept)."""
    fd = FileData(path, "subagent" if is_subagent_path(root, path) else "main")
    fd.tool_info = {}
    pending = []
    seen = set()
    for obj in read_records(path):
        if obj is None:
            continue
        t, ts = obj.get("type"), obj.get("timestamp")
        if isinstance(ts, str) and (fd.first_ts is None or ts < fd.first_ts):
            fd.first_ts = ts
        cwd = obj.get("cwd")
        if fd.project is None and isinstance(cwd, str) and cwd:
            fd.project = cwd
        if t == "api-request":
            if isinstance(obj.get("id"), str) and isinstance(obj.get("shapeHash"), str):
                fd.req_shape[obj["id"]] = obj["shapeHash"]
        elif t == "api-request-shape":
            h, shape = obj.get("shapeHash"), obj.get("shape")
            if isinstance(h, str) and isinstance(shape, dict) and h not in fd.shapes:
                fd.shapes[h] = shape_metrics(shape)
                fd.shape_order.append(h)
                for name, n in fd.shapes[h]["defs"].items():
                    fd.defs[name] = n
        elif t == "cost-state":
            fd.cost_state = obj
        elif t in ("user", "assistant", "attachment", "system"):
            uuid = obj.get("uuid")
            if not isinstance(uuid, str) or uuid in seen:
                continue
            seen.add(uuid)
            parent = obj.get("parentUuid")
            rec = Rec(len(fd.recs), uuid, parent if isinstance(parent, str) else None,
                      ts if isinstance(ts, str) else None)
            fd.recs.append(rec)
            fd.uuid_idx[uuid] = rec.pos
            if t == "assistant":
                parse_assistant(fd, rec, obj, rec.ts)
            elif t == "user":
                parse_user(fd, rec, obj, pending)
            elif t == "attachment":
                parse_attachment(fd, rec, obj)
            elif obj.get("subtype") == "compact_boundary":
                rec.kind, rec.label = "B", "S:compact_boundary"
                meta = obj.get("compactMetadata")
                pm = meta.get("preservedMessages") if isinstance(meta, dict) else None
                uu = pm.get("uuids") if isinstance(pm, dict) else None
                rec.preserved = [u for u in uu if isinstance(u, str)] if isinstance(uu, list) else []
    for rec in fd.recs:
        rec.pp = fd.uuid_idx.get(rec.parent) if rec.parent else None
    # tool results are sized after the whole file is read, so that tool_reference blocks
    # can use the tool definitions of the whole session
    for rec, b in pending:
        tid = b.get("tool_use_id")
        info = fd.tool_info.get(tid)
        name, group, meta, tpath = info if info else (None, "other", "-", None)
        chars, images, unsized = 0, [], 0
        inner = b.get("content")
        if isinstance(inner, str):
            chars = len(inner)
        elif isinstance(inner, list):
            for ib in inner:
                if not isinstance(ib, dict):
                    continue
                it = ib.get("type")
                if it == "text":
                    tx = ib.get("text")
                    chars += len(tx) if isinstance(tx, str) else 0
                elif it == "image":
                    images.append(image_dims(ib))
                elif it == "tool_reference":
                    d = fd.defs.get(ib.get("tool_name"))
                    if d is None:
                        unsized += 1
                    else:
                        chars += d
        item = Item("tool result", group, chars, [rec], meta=meta, path=tpath)
        item.unsized_refs = unsized
        item.tool, item.tid, item.is_error = name, tid, b.get("is_error") is True
        fd.add_item(item)
        for dims in images:
            fd.add_item(Item("image", "from tool result", 0, [rec], img=dims, meta=meta))
        if isinstance(tid, str):
            fd.result_rec[tid] = rec.pos
    return fd


# ----------------------------------------------------------------- steps, presence

class Step:
    """What happened between call k-1 and call k."""

    def __init__(self, k, kind):
        self.k = k
        self.kind = kind            # first, compaction, restart, rule_a, normal
        self.boundary = None
        self.new_recs = []
        self.preserved_prev = []    # preserved records that were already in the context before
        self.present_extra = []     # items of those records
        self.shape_changed = False
        self.shape_item = None
        self.shrink = False
        self.model_changed = False
        self.excl = None            # reason this step's difference went to the residual
        self.T = 0                  # C_k - C_(k-1)
        self.N = 0.0                # T minus the output items that entered
        self.U = 0.0                # output of call k-1 that no item took
        self.nres = 0.0             # tokens this step adds to the residual
        self.late = 0.0
        self.sample = None


def build_steps(fd):
    """Order calls, find each call's predecessor and set Rec.enter (amendment 1, A, B, C)."""
    recs, calls = fd.recs, fd.calls
    mc = [None] * len(recs)         # highest call index of an assistant record on the chain
    bd = [None] * len(recs)         # nearest compaction boundary on the chain

    def resolve(ri):
        stack, cur = [], ri
        while cur is not None and mc[cur] is None and len(stack) <= len(recs):
            stack.append(cur)
            cur = recs[cur].pp
        base_m, base_b = (mc[cur], bd[cur]) if cur is not None and mc[cur] is not None else (-1, None)
        for r in reversed(stack):
            rc = recs[r]
            base_m = max(base_m, rc.call if rc.kind == "A" else -1)
            base_b = r if rc.kind == "B" else base_b
            mc[r], bd[r] = base_m, base_b

    chain_done = [False] * len(recs)
    touched = set()
    steps = []
    prev_b = None
    for k, call in enumerate(calls):
        start = call.recs[0].pp
        if start is None:
            m, b = -1, None
        else:
            resolve(start)
            m, b = mc[start], bd[start]
        if k == 0:
            kind = "first"
        elif b is not None and b != prev_b:
            kind = "compaction"
        elif m < 0:
            kind = "restart"
        elif m != k - 1:
            kind = "rule_a"
        else:
            kind = "normal"
        step = Step(k, kind)
        step.boundary = b
        cur = start
        while cur is not None and not chain_done[cur]:
            chain_done[cur] = True
            rc = recs[cur]
            if rc.enter is None:
                rc.enter = k
                step.new_recs.append(rc)
            # B: the other records of a message that is on the chain, and the results of its tools
            if rc.kind == "A" and rc.call < k and rc.call not in touched:
                touched.add(rc.call)
                other = calls[rc.call]
                for r2 in other.recs:
                    if r2.enter is None:
                        r2.enter = k
                        step.new_recs.append(r2)
                for tid in other.tool_ids:
                    rp = fd.result_rec.get(tid)
                    if rp is not None and recs[rp].enter is None:
                        recs[rp].enter = k
                        step.new_recs.append(recs[rp])
            cur = rc.pp
        if kind in FIRST_LIKE and b is not None and (k == 0 or b != prev_b):
            # C: records the compaction kept stay in the context after the boundary
            for u in recs[b].preserved:
                ri = fd.uuid_idx.get(u)
                if ri is None or recs[ri].kind not in ("A", "U", "T"):
                    continue
                rc = recs[ri]
                if rc.enter is None:
                    rc.enter = k
                    step.new_recs.append(rc)
                elif rc.enter < k:
                    step.preserved_prev.append(rc)
        prev_b = b
        steps.append(step)
    return steps


def size_outputs(fd, call):
    """Sizes of the output items of one call (plan section 3: thinking exact, the rest by characters)."""
    out = call.u["out"]
    think = min(call.u["think"], out) if call.u["think"] else 0
    if call.think_item is None and (think > 0 or call.think_recs):
        recs = call.think_recs or [call.recs[0]]
        call.think_item = fd.add_item(Item("thinking", "-", 0, recs, out_call=call.idx))
    if call.think_item is not None:
        call.think_item.size = float(think)
    rest = float(out - think)
    items = call.out_items
    if items:
        total = sum(it.chars for it in items)
        for it in items:
            it.size = rest * it.chars / total if total > 0 else rest / len(items)
        call.unattached = 0.0
    else:
        call.unattached = rest              # output with no record to carry it: goes to the residual


def allocate(others, n_tokens):
    """Split n_tokens over the new items of a step (plan section 3). False when nothing can take them."""
    if not others:
        return n_tokens == 0
    if len(others) == 1:
        others[0].size = float(n_tokens)           # a step with one item: exact
        return True
    if n_tokens == 0:
        for it in others:
            it.size = 0.0
        return True
    imgs = [it for it in others if it.img]
    est = [it.img[0] * it.img[1] / IMAGE_DIVISOR for it in imgs]
    txt = [it for it in others if not it.img]
    tchars = sum(it.chars for it in txt)
    sum_img = sum(est)
    if imgs and (sum_img >= n_tokens or tchars == 0):
        scale = n_tokens / sum_img                   # the images take all of it, in proportion
        for it, e in zip(imgs, est):
            it.size = e * scale
        for it in txt:
            it.size = 0.0
        return True
    if tchars == 0:
        return False
    for it, e in zip(imgs, est):
        it.size = e
    rest = n_tokens - sum_img
    for it in txt:
        it.size = rest * it.chars / tchars
    return True


class Analysis:
    def __init__(self, fd, steps):
        self.fd = fd
        self.steps = steps
        self.samples = []           # (category, tool, chars, tokens) of single-item steps
        self.epoch_end = []
        self.by_enter = defaultdict(list)
        self.cb = []                # cb[k]: counted calls among calls[:k]
        self.shape_of = []          # shape hash of each call, or None
        self.excl = Counter()
        self.late_items = 0


def call_shapes(fd):
    out, last = [], None
    for c in fd.calls:
        h = fd.req_shape.get(c.req_ref) if c.req_ref else None
        h = h if h in fd.shapes else None
        h = h or last
        out.append(h)
        last = h or last
    first = next((h for h in out if h), None) or (fd.shape_order[0] if fd.shape_order else None)
    return [h or first for h in out]


def analyze_file(fd, counted):
    """Stages 1 to 5: presence, steps, item sizes. Returns an Analysis, or None for a file with no calls."""
    calls = fd.calls
    n = len(calls)
    if n == 0:
        return None
    for c, flag in zip(calls, counted):
        c.counted = flag
    steps = build_steps(fd)
    an = Analysis(fd, steps)
    for c in calls:
        size_outputs(fd, c)
    an.shape_of = call_shapes(fd)
    for k in range(1, n):
        st_ = steps[k]
        h0, h1 = an.shape_of[k - 1], an.shape_of[k]
        st_.model_changed = isinstance(calls[k].model, str) and isinstance(calls[k - 1].model, str) \
            and calls[k].model != calls[k - 1].model
        if st_.kind not in FIRST_LIKE and h0 and h1 and h0 != h1:
            st_.shape_changed = True
            delta = fd.shapes[h1]["total"] - fd.shapes[h0]["total"]
            if delta > 0:
                st_.shape_item = fd.add_item(Item("prefix", "shape change", delta, []))
                st_.shape_item.enter = k
            else:
                st_.shrink = True
    # epochs: a first, compaction or restart call starts a new context
    starts = [s.k for s in steps if s.kind in FIRST_LIKE]
    an.epoch_end = [0] * n
    for i, s0 in enumerate(starts):
        end = starts[i + 1] - 1 if i + 1 < len(starts) else n - 1
        for k in range(s0, end + 1):
            an.epoch_end[k] = end
    for it in fd.items:
        if it.recs:
            ents = [r.enter for r in it.recs if r.enter is not None]
            it.enter = min(ents) if ents else None
        if it.enter is not None:
            it.segs = [(it.enter, an.epoch_end[it.enter])]
            an.by_enter[it.enter].append(it)
    for s in steps:
        seen = set()
        for rc in s.preserved_prev:
            for it in rc.items:
                if id(it) not in seen and it.enter is not None and it.enter < s.k:
                    seen.add(id(it))
                    it.segs.append((s.k, an.epoch_end[s.k]))
                    s.present_extra.append(it)
    an.cb = [0] * (n + 1)
    for k in range(n):
        an.cb[k + 1] = an.cb[k] + (1 if calls[k].counted else 0)
    # normal steps: N_k = C_k - C_(k-1) - O_(k-1), split over the new items
    for k in range(1, n):
        s = steps[k]
        if s.kind in FIRST_LIKE:
            continue
        prev = calls[k - 1]
        s.T = calls[k].context - prev.context
        new_items = an.by_enter.get(k, [])
        outs_prev = [it for it in new_items if it.out_call == k - 1]
        late = [it for it in new_items if it.out_call is not None and it.out_call != k - 1]
        others = [it for it in new_items if it.out_call is None]
        s.U = max(0.0, prev.u["out"] - sum(it.size or 0.0 for it in outs_prev))
        s.late = sum(it.size or 0.0 for it in late)
        an.late_items += len(late)
        s.N = s.T - s.late - prev.u["out"]
        reason = None
        if s.kind == "rule_a":
            reason = "fork (not the previous call)"
        elif s.model_changed:
            reason = "model switch"
        elif s.N < 0:
            reason = "negative N"
        elif s.shrink:
            reason = "shape shrink"
        if reason is None:
            if not allocate(others, s.N):
                reason = "no sizable item"
        if reason:
            s.excl = reason
            an.excl[reason] += 1
            for it in others:
                it.size, it.unsized = 0.0, True
            s.nres = s.U + s.N
        else:
            s.nres = s.U
            if len(others) == 1 and others[0].chars > 0 and not others[0].img \
                    and others[0] is not s.shape_item and s.N >= MIN_SINGLE_TOKENS:
                it = others[0]
                an.samples.append((it.cat, it.sub, it.chars, s.N))
    return an


# ------------------------------------------------------------- weights and totals

def call_rates(call):
    """(input, 5m write, 1h write, read, output) in USD per token for a call, or None when unpriced."""
    p = price_for(call.model)
    if p is None:
        return None
    m = 1e-6
    return (p["input"] * m, p["w5m"] * m, p["w1h"] * m, p["read"] * m, p["output"] * m)


def call_split(call):
    """(uncached input, 5m writes, 1h writes, cache reads, output) of a call."""
    u = call.u
    cw1 = u["cw1"] if u["has_split"] else 0
    cw1 = min(cw1, u["cw"])
    return u["input"], u["cw"] - cw1, cw1, u["cr"], u["out"]


def write_rate(call, rates):
    """Average weight of the call's non-read input tokens."""
    inp, cw5, cw1, _cr, _out = call_split(call)
    tokens = inp + cw5 + cw1
    if tokens <= 0:
        return rates[0]
    return (inp * rates[0] + cw5 * rates[1] + cw1 * rates[2]) / tokens


def cache_lifetime(calls, k):
    """Seconds the previous call's cache write lasts: 1 hour if it wrote a 1h entry, else 5 minutes."""
    for j in range(k - 1, -1, -1):
        u = calls[j].u
        if u["has_split"]:
            if u["cw1"] > 0:
                return 3600.0
            if u["cw5"] > 0:
                return 300.0
    return 300.0


def rebuild_cause(an, k):
    """First cause that applies (amendment 1, F): model change, shape change, idle gap, other."""
    s, calls = an.steps[k], an.fd.calls
    if s.model_changed:
        return "model change"
    if s.shape_changed:
        return "shape change"
    t1, t0 = parse_ts(calls[k].ts), parse_ts(calls[k - 1].ts)
    if t1 is not None and t0 is not None and t1 - t0 > cache_lifetime(calls, k):
        return "idle gap"
    return "other"


def cost_state_cost(cs, share_1h=0.0):
    """Weighted cost of the token totals in a cost-state record, the cost it reports, web searches.

    Older records have no 5m/1h split of the cache writes; share_1h (the share of 1h writes among the
    logged calls of the same session) is applied to them."""
    mu = cs.get("modelUsage") if isinstance(cs, dict) else None
    weighted = reported = 0.0
    web = unpriced = 0
    for model, m in (mu.items() if isinstance(mu, dict) else []):
        if not isinstance(m, dict):
            continue
        reported += m.get("costUSD") or 0.0
        web += m.get("webSearchRequests") or 0
        p = price_for(model)
        if p is None:
            unpriced += 1
            continue
        cw = m.get("cacheCreationInputTokens") or 0
        cw1 = m.get("cacheCreationEphemeral1hInputTokens")
        cw5 = m.get("cacheCreationEphemeral5mInputTokens")
        if cw1 is None and cw5 is None:
            cw1 = cw * share_1h
            cw5 = cw - cw1
        elif cw1 is None:
            cw1 = max(0, cw - cw5)
        elif cw5 is None:
            cw5 = max(0, cw - cw1)
        weighted += ((m.get("inputTokens") or 0) * p["input"] + cw5 * p["w5m"] + cw1 * p["w1h"]
                     + (m.get("cacheReadInputTokens") or 0) * p["read"]
                     + (m.get("outputTokens") or 0) * p["output"]) * 1e-6
    total = cs.get("totalCostUSD") if isinstance(cs, dict) else None
    return weighted, (total if isinstance(total, (int, float)) else reported), web, unpriced


def estimate_size(item, tokens_per_char):
    if item.img:
        return item.img[0] * item.img[1] / IMAGE_DIVISOR
    return item.chars * tokens_per_char


def prefix_split(an, k):
    """Share of the prefix by system text, built-in tools, one group per MCP server, other tools."""
    h = an.shape_of[k]
    m = an.fd.shapes.get(h) if h else None
    if not m or m["total"] <= 0:
        return {"unsplit (no shape logged)": 1.0}
    out = {}
    if m["sys"]:
        out["system text"] = m["sys"] / m["total"]
    for label, chars in m["groups"].items():
        out[label] = out.get(label, 0.0) + chars / m["total"]
    return out


Session = SimpleNamespace   # the numbers of one analyzed file


def finish_file(an, pooled_ratio):
    """Stages 6 and 7: estimate first-call items, residual, weighted cost, rows."""
    fd, steps, calls = an.fd, an.steps, an.fd.calls
    n = len(calls)
    own = [chars / tokens for (_c, _s, chars, tokens) in an.samples]
    if own:
        ratio, ratio_src = statistics.median(own), "session"
    elif pooled_ratio:
        ratio, ratio_src = pooled_ratio, "pooled"
    else:
        ratio, ratio_src = FALLBACK_CHARS_PER_TOKEN, "fallback"
    tpc = 1.0 / ratio
    C = [c.context for c in calls]
    rates = [call_rates(c) if c.counted else None for c in calls]
    unpriced = sum(1 for c, r in zip(calls, rates) if c.counted and r is None)
    wr_cum = [0.0] * n                  # read weight summed over counted, priced, non-first-like calls
    run = 0.0
    for k in range(n):
        if calls[k].counted and rates[k] and steps[k].kind not in FIRST_LIKE:
            run += rates[k][3]
        wr_cum[k] = run
    an.wr_cum, an.rates = wr_cum, rates     # hive: kept for the replay screen

    rho, rho_list = 0.0, [0.0] * n
    res = {"new": 0.0, "carried": 0.0, "out": 0.0, "w": 0.0}
    rebuild_w, rebuild_tok, rebuild_n = Counter(), Counter(), Counter()
    rebuild_big = defaultdict(lambda: [0, 0, 0.0])    # cause -> calls, tokens, cost of rebuilds >= BIG_REBUILD
    rebuild_events = []                               # (tokens, cause, call, gap seconds, lifetime seconds)
    sum_w_calls = 0.0
    prefix_first = None
    for k in range(n):
        c, s, r = calls[k], steps[k], rates[k]
        cnt = c.counted
        inp, cw5, cw1, cr, out = call_split(c)
        if s.kind in FIRST_LIKE:
            present = list(an.by_enter.get(k, [])) + s.present_extra
            for it in present:
                if it.size is None:
                    it.size, it.estimated = estimate_size(it, tpc), True
            tot = sum(it.size for it in present)
            prefix = max(0.0, C[k] - tot)
            pit = Item("prefix", "system+tools", 0, [], size=prefix)
            pit.enter, pit.segs = k, [(k, an.epoch_end[k])]
            pit.split = prefix_split(an, k)
            fd.items.append(pit)
            an.by_enter[k].append(pit)
            if prefix_first is None and cnt:
                prefix_first = (prefix, an.shape_of[k])
            rho = C[k] - tot - prefix
            if cnt:
                res["new"] += rho
            if cnt and r:
                w_hat = write_rate(c, r)
                reads, spent = cr, 0.0
                comps = sorted(present, key=lambda it: min((x.pos for x in it.recs), default=1 << 60))
                for it in [pit] + comps:
                    take = min(it.size, reads)
                    reads -= take
                    cost = take * r[3] + (it.size - take) * w_hat
                    it.w += cost
                    spent += cost
                res["w"] += (cr * r[3] + inp * r[0] + cw5 * r[1] + cw1 * r[2]) - spent
        else:
            if cnt:
                res["carried"] += rho
                res["new"] += s.nres
            if cnt and r:
                res["w"] += rho * r[3]
                w_hat = write_rate(c, r)
                carried_cost = C[k - 1] * r[3]
                rebuilt = max(0, C[k - 1] - cr)
                rebuild_cost = rebuilt * (w_hat - r[3])
                new_cost = cr * r[3] + inp * r[0] + cw5 * r[1] + cw1 * r[2] - carried_cost - rebuild_cost
                new_items = [it for it in an.by_enter.get(k, []) if it.size]
                tot_s = sum(it.size for it in new_items)
                if s.excl:
                    frac = min(1.0, tot_s / s.T) if s.T > 0 else 0.0
                else:
                    frac = 1.0
                alloc = new_cost * frac if tot_s > 0 else 0.0
                for it in new_items:
                    it.w += alloc * it.size / tot_s
                res["w"] += new_cost - alloc
                if rebuilt > 0:
                    cause = rebuild_cause(an, k)
                    rebuild_w[cause] += rebuild_cost
                    rebuild_tok[cause] += rebuilt
                    rebuild_n[cause] += 1
                    if rebuilt >= BIG_REBUILD:
                        rebuild_big[cause] = [a + b for a, b in zip(rebuild_big[cause], (1, rebuilt, rebuild_cost))]
                        t1, t0 = parse_ts(c.ts), parse_ts(calls[k - 1].ts)
                        rebuild_events.append((rebuilt, cause, k, None if t1 is None or t0 is None else t1 - t0,
                                               cache_lifetime(calls, k)))
            rho += s.nres
        rho_list[k] = rho
        # the call's own output
        outs = c.out_items + ([c.think_item] if c.think_item is not None else [])
        if cnt:
            res["out"] += c.unattached
            if r:
                res["w"] += c.unattached * r[4]
                for it in outs:
                    it.w += it.size * r[4]
                sum_w_calls += (inp * r[0] + cw5 * r[1] + cw1 * r[2] + cr * r[3] + out * r[4])
    # raw and carried-weighted totals per item
    sum_item_tokens = 0.0
    for it in fd.items:
        if it.size is None:
            it.size = 0.0
        pc = sum(an.cb[b + 1] - an.cb[a] for a, b in it.segs)
        entered = it.enter is not None and calls[it.enter].counted
        it.new = it.size if entered else 0.0
        it.carried_calls = pc - (1 if entered else 0)
        it.carried = it.size * it.carried_calls
        it.w += it.size * sum(wr_cum[b] - wr_cum[a] for a, b in it.segs)
        if it.out_call is not None and calls[it.out_call].counted:
            it.out = it.size
        sum_item_tokens += it.size * pc

    ses = Session()
    ses.fd, ses.an = fd, an
    ses.kind, ses.sid, ses.project = fd.kind, fd.sid, fd.project
    counted_calls = [c for c in calls if c.counted]
    ses.calls, ses.inherited = len(counted_calls), n - len(counted_calls)
    ses.totals = {"input": sum(c.u["input"] for c in counted_calls), "cw": sum(c.u["cw"] for c in counted_calls),
                  "cr": sum(c.u["cr"] for c in counted_calls), "out": sum(c.u["out"] for c in counted_calls)}
    ses.all_totals = {"input": sum(c.u["input"] for c in calls), "cw": sum(c.u["cw"] for c in calls),
                      "cr": sum(c.u["cr"] for c in calls), "out": sum(c.u["out"] for c in calls)}
    ses.sum_context = sum(C[k] for k in range(n) if calls[k].counted)
    ses.sum_out = ses.totals["out"]
    ses.residual_abs = sum(abs(rho_list[k]) for k in range(n) if calls[k].counted)
    ses.residual_share = ses.residual_abs / ses.sum_context if ses.sum_context else 0.0
    ses.ident_err = ses.sum_context - (sum_item_tokens + sum(rho_list[k] for k in range(n) if calls[k].counted))
    ses.ratio, ses.ratio_src = ratio, ratio_src
    ses.unpriced = unpriced
    ses.prefix_first = prefix_first
    ses.kinds = Counter(s.kind for s in steps)
    ses.excl = an.excl
    ses.rebuild_w, ses.rebuild_tok, ses.rebuild_n = rebuild_w, rebuild_tok, rebuild_n
    ses.rebuild_big = dict(rebuild_big)
    ses.rebuild_events = rebuild_events
    ses.unsized_refs = sum(it.unsized_refs for it in fd.items)
    ses.never = Counter(r.label for r in fd.recs if r.kind in ("A", "U", "T") and r.enter is None)
    ses.synthetic = fd.synthetic
    ses.late_items = an.late_items
    ses.weighted_calls = sum_w_calls
    ses.date = (fd.first_ts or "")[:10]
    ses.cost_check = None
    ses.unlogged = None
    if fd.cost_state is not None:
        w5 = sum(c.u["cw5"] for c in counted_calls if c.u["has_split"])
        w1 = sum(c.u["cw1"] for c in counted_calls if c.u["has_split"])
        weighted, reported, web, unpriced_models = cost_state_cost(fd.cost_state, w1 / (w5 + w1) if w5 + w1 else 0.0)
        ses.cost_check = (weighted, reported, web, unpriced_models)
        ses.unlogged = weighted - sum_w_calls
    rows = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for it in fd.items:
        vals = (it.new, it.carried, it.out, it.w)
        if it.split:
            for label, share in it.split.items():
                row = rows[("prefix", label)]
                for i in range(4):
                    row[i] += vals[i] * share
        else:
            row = rows[(it.cat, it.sub)]
            for i in range(4):
                row[i] += vals[i]
    row = rows[("residual", "unattributed")]
    row[0] += res["new"]
    row[1] += res["carried"]
    row[2] += res["out"]
    row[3] += res["w"]
    for cause, v in rebuild_w.items():
        rows[("cache rebuild", cause)][3] += v
    ses.rows = dict(rows)
    return ses


# --------------------------------------------------------------------- sessions

def short_project(project):
    """The last folder name of a working folder, nothing else."""
    if not project:
        return "<unknown>"
    parts = [p for p in str(project).replace("\\", "/").split("/") if p]
    return parts[-1] if parts else "<unknown>"


def session_date(fd, path):
    if fd.first_ts:
        return fd.first_ts[:10]
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")


def load_sessions(dirs, since=None, project=None):
    """Parse every file, give each message id to the earliest file (amendment 1, G), analyze the
    files that pass the filters, and return their Session objects."""
    parsed = []
    for i, (root, path) in enumerate(find_logs(dirs)):
        fd = parse_file(root, path)
        fd.index = i
        parsed.append(fd)
    owner = {}
    for fd in parsed:
        key = (fd.first_ts or "", fd.index)
        for c in fd.calls:
            cur = owner.get(c.mid)
            if cur is None or key < cur[0]:
                owner[c.mid] = (key, fd.index)
    analyses = []
    for pos, fd in enumerate(parsed):
        parsed[pos] = None
        if not fd.calls:
            continue
        if since and session_date(fd, fd.path) < since:
            continue
        if project and norm_project(project) not in norm_project(fd.project or fd.path.parent.name):
            continue
        counted = [owner[c.mid][1] == fd.index for c in fd.calls]
        an = analyze_file(fd, counted)
        if an is not None:
            fd.date = session_date(fd, fd.path)
            analyses.append(an)
    samples = [chars / tokens for an in analyses for (_c, _s, chars, tokens) in an.samples]
    pooled = statistics.median(samples) if samples else None
    sessions = []
    for an in analyses:
        ses = finish_file(an, pooled)
        ses.date = an.fd.date
        ses.path = an.fd.path
        ses.project_name = short_project(an.fd.project or an.fd.path.parent.name)
        sessions.append(ses)
    sessions.sort(key=lambda s: (s.date, s.sid))
    return sessions

def compartment(cat, sub):
    """Grouping of plan section 7 (the amendment moves model-change rebuilds to model and effort choice)."""
    if cat in ("tool input", "tool result"):
        return "agentic work"
    if cat in ("prompt", "assistant text", "injected", "compaction summary"):
        return "chat text"
    if cat == "thinking" or (cat == "cache rebuild" and sub == "model change"):
        return "model and effort choice"
    if cat == "image":
        return "vision input"
    if cat == "prefix":
        return "prefix (fixed cost, not a candidate)"
    if cat == "cache rebuild":
        return "cache rebuild, other causes (not a compartment)"
    return "residual (no source)"


def merge_rows(sessions):
    total = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for s in sessions:
        for key, vals in s.rows.items():
            row = total[key]
            for i in range(4):
                row[i] += vals[i]
    return dict(total)
