"""Synthetic Claude Code session logs for the tests. No real log is read anywhere in the tests.

The form is the one Claude Code writes: one JSON object per line, one assistant record per content
block, copies of one message id carrying the same usage, tool results as user records.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def use(inp=0, cw=0, cr=0, out=0, think=0, cw1=None):
    cw1 = cw if cw1 is None else cw1
    return {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
            "output_tokens": out, "cache_creation": {"ephemeral_5m_input_tokens": cw - cw1,
                                                      "ephemeral_1h_input_tokens": cw1},
            "output_tokens_details": {"thinking_tokens": think}}


def tool_use(tid, name="Read", inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp if inp is not None else {"file_path": "a.md"}}


class Log:
    def __init__(self, start="2026-11-01T10:00:00", cwd="D:\\Dev\\Demo Project", entrypoint="cli",
                 version="2.1.319", sidechain=False):
        self.recs, self.n, self.cwd = [], 0, cwd
        self.entrypoint, self.version, self.sidechain = entrypoint, version, sidechain
        self.t = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)

    def ts(self, add=1.0):
        self.t += timedelta(seconds=add)
        return self.t.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def push(self, obj, add=1.0):
        self.n += 1
        obj.setdefault("uuid", "u%04d" % self.n)
        obj.setdefault("timestamp", self.ts(add))
        obj.setdefault("cwd", self.cwd)
        obj.setdefault("version", self.version)
        if self.entrypoint:
            obj.setdefault("entrypoint", self.entrypoint)
        if self.sidechain:
            obj.setdefault("isSidechain", True)
        self.recs.append(obj)
        return obj["uuid"]

    def user(self, content, parent=None, **extra):
        return self.push({"type": "user", "parentUuid": parent, "message": {"role": "user", "content": content},
                          **extra})

    def result(self, tid, content, parent, is_error=None, **extra):
        block = {"type": "tool_result", "tool_use_id": tid, "content": content}
        if is_error is not None:
            block["is_error"] = is_error
        return self.push({"type": "user", "parentUuid": parent, "toolUseResult": {"x": 1},
                          "message": {"role": "user", "content": [block]}, **extra})

    def call(self, mid, blocks, usage, parent, model=OPUS, add=1.0):
        uuids = []
        for i, b in enumerate(blocks):
            u = self.push({"type": "assistant", "parentUuid": uuids[-1] if uuids else parent,
                           "message": {"id": mid, "model": model, "role": "assistant", "content": [b],
                                       "usage": usage}}, add if i == 0 else 0.1)
            uuids.append(u)
        return uuids

    def boundary(self, preserved=()):
        return self.push({"type": "system", "subtype": "compact_boundary", "parentUuid": None,
                          "compactMetadata": {"trigger": "auto", "preservedMessages": {"uuids": list(preserved)}}})

    def write(self, root, folder="P", name="aaaaaaaa-1"):
        path = Path(root) / folder / (name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in self.recs) + "\n", encoding="utf-8")
        return path


def chain3(start="2026-11-01T10:00:00", cwd="D:\\Dev\\Demo Project", prompt="x" * 5, entrypoint="cli"):
    """Prompt, Read, result (1,000 chars), Bash, result (100 chars), final text.
    Calls: C = 10,000 / 10,530 / 10,600; O = 30 / 20 / 5; all on Opus 5.5 with 1-hour cache writes."""
    log = Log(start, cwd, entrypoint)
    u1 = log.user(prompt)
    a1 = log.call("m1" + start, [tool_use("t1" + start)], use(cw=10000, out=30), u1)
    r1 = log.result("t1" + start, "x" * 1000, a1[-1])
    a2 = log.call("m2" + start, [tool_use("t2" + start, "Bash", {"command": "ls -la"})],
                  use(cw=530, cr=10000, out=20), r1)
    r2 = log.result("t2" + start, "y" * 100, a2[-1])
    log.call("m3" + start, [{"type": "text", "text": "ok"}], use(cw=70, cr=10530, out=5), r2)
    return log


# Cost of chain3 at api-v1 (Opus 5.5: 1h write 8, read 0.20, output 20 USD per million tokens):
#   call 1: 10,000 * 8 + 30 * 20                  =  80,600 micro-USD
#   call 2: 530 * 8 + 10,000 * 0.2 + 20 * 20      =   6,640
#   call 3: 70 * 8 + 10,530 * 0.2 + 5 * 20        =   2,766
CHAIN3_COST = (80_600 + 6_640 + 2_766) / 1e6
