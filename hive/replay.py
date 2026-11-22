"""Replay screen `bash_output_limit`, version 2 (plan section 4). No model call; nothing is written.

For each successful Bash or PowerShell result longer than the limit (the setting bashOutputMaxChars
covers both), the kept text is what Claude Code shows instead: the first PREVIEW_CHARS characters and the
path of the file that holds the rest (Claude Code tools documentation, read 2026-11-12). A result is
"needed" when a word of 8 or more characters from [A-Za-z0-9_./\\:-] that occurs in the removed part,
and not in the kept part or the command, appears in the next NEXT_RECORDS assistant records (text or
tool input) and occurs in no earlier record of the session file (any string value of a record before
the one that holds the result). This needed rate is a lower bound: it misses a result needed for a word
that was already in the session, for example after a compaction.

Version 1 had no "earlier record" condition. Its rule matched in 41% to 50% of the cases on records
written before the result existed, so its needed rate counts words in circulation; it is reported as the
upper bound (`needed_upper`) and is not part of the pass rule (eval/hive/STEP5_DECISIONS.md section 1).

- A result that is not needed saves its removed tokens times the later calls it stays in context, at
  each call's cache-read price (the carried read weight of the breakdown).
- A needed result saves nothing and costs one extra call at the next call's context, at that call's
  cache-read price.
- Pass: net saving at least PASS_SAVING of the node's weighted cost in the period, and needed results
  (version 2's rule) at most PASS_NEEDED of cut results. Of the limits that pass, the one with the
  largest net saving is proposed.

The tokens of the removed part are the result's tokens times the removed share of its characters.

Also reported, not part of the pass rule: the weighted cost of all Bash and PowerShell results in the period
(the component of the catalog entry, `component_total`) and, per limit, the saving as a share of it
(`component_saving_share`). One minus that share estimates the component's ratio on / off; it leaves out
output read again after a cut, which the trial's component ratio measures.
"""
import re
from datetime import datetime, timedelta, timezone

from . import breakdown as tb
from .guards import SHELL

REPLAY_CODE = "2"
PREVIEW_CHARS = 2000
NEXT_RECORDS = 10
PASS_SAVING = 0.010
PASS_NEEDED = 0.25
WORD = re.compile(r"[A-Za-z0-9_./\\:-]{8,}")
PERIOD_DAYS = 28


def result_text(block):
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return "".join(ib.get("text", "") for ib in inner
                       if isinstance(ib, dict) and ib.get("type") == "text" and isinstance(ib.get("text"), str))
    return ""


def strings(obj):
    """Every string value of a parsed JSON record (not its keys), depth first."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def raw_pass(path, tids):
    """({tool_use id: (result text, [texts of the next assistant records], end)}, history) for the given ids.

    Read in file order; an assistant record's text is its text blocks and the JSON of its tool inputs.
    history is every string value of every record, joined by newlines; end is its length before the
    record that holds the result, so history[:end] holds exactly the records before it. A word of WORD has
    no newline, so it occurs in an earlier record when history.find(word, 0, end) finds it."""
    pending, out, seen, earlier, pos = {}, {}, set(), [], 0
    for obj in tb.read_records(path):
        if obj is None:
            continue
        uid = obj.get("uuid")
        if isinstance(uid, str):
            if uid in seen:
                continue
            seen.add(uid)
        end = pos
        for value in strings(obj):
            earlier.append(value)
            pos += len(value) + 1
        t, msg = obj.get("type"), obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if t == "assistant" and pending:
            parts = []
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif isinstance(b, dict) and b.get("type") == "tool_use":
                    parts.append(tb.cjson(b.get("input")))
            if isinstance(content, str):
                parts.append(content)
            text = "\n".join(parts)
            for tid in list(pending):
                pending[tid][1].append(text)
                if len(pending[tid][1]) >= NEXT_RECORDS:
                    out[tid] = pending.pop(tid)
        elif t == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in tids \
                        and b["tool_use_id"] not in out and b["tool_use_id"] not in pending:
                    pending[b["tool_use_id"]] = (result_text(b), [], end)
    out.update(pending)
    return out, "\n".join(earlier)


def named_words(text, command, later):
    """Words of the removed part, not in the kept part or the command, that a later record names."""
    kept, removed = text[:PREVIEW_CHARS], text[PREVIEW_CHARS:]
    words = {w for w in WORD.findall(removed) if w not in kept and w not in command}
    return {w for w in words if any(w in rec for rec in later)}


def needed(text, command, later, history="", end=0):
    """(version 2, version 1) for one cut result. Version 2, the lower bound: a named word occurs in no
    earlier record (history[:end]). Version 1, the upper bound: any named word."""
    named = named_words(text, command, later)
    return any(history.find(w, 0, end) < 0 for w in named), bool(named)


def carried_rate(an, it):
    """Sum of the cache-read price over the later counted calls the item stays in (USD per token)."""
    return sum(an.wr_cum[b] - an.wr_cum[a] for a, b in it.segs)


def screen(sessions, limits):
    """Per limit: cut results, needed results, saving, extra cost, net saving and its share, pass."""
    total = sum(v[3] for s in sessions for v in s.rows.values())
    component = sum(it.w for s in sessions for it in s.fd.items if it.cat == "tool result" and it.tool in SHELL)
    cases = []
    for s in sessions:
        an = s.an
        items = [it for it in s.fd.items if it.cat == "tool result" and it.tool in SHELL and not it.is_error
                 and it.tid and it.chars > min(limits) and it.enter is not None]
        if not items:
            continue
        texts, history = raw_pass(s.path, {it.tid for it in items})
        for it in items:
            text, later, end = texts.get(it.tid, ("", [], 0))
            if len(text) != it.chars:
                continue                       # the text read here is not the one the breakdown sized
            info = s.fd.tool_info.get(it.tid)
            command = info[3] if info and isinstance(info[3], str) else ""
            k = it.enter
            call = s.fd.calls[k]
            rate = an.rates[k][3] if call.counted and an.rates[k] else 0.0
            lower, upper = needed(text, command, later, history, end)
            cases.append({"chars": it.chars, "size": it.size, "carried_rate": carried_rate(an, it),
                          "needed": lower, "needed_upper": upper, "extra": call.context * rate})
    out = {}
    for lim in limits:
        cut = [c for c in cases if c["chars"] > lim]
        saving = sum(c["size"] * (c["chars"] - PREVIEW_CHARS) / c["chars"] * c["carried_rate"]
                     for c in cut if not c["needed"])
        extra = sum(c["extra"] for c in cut if c["needed"])
        n_needed = sum(1 for c in cut if c["needed"])
        n_upper = sum(1 for c in cut if c["needed_upper"])
        net = saving - extra
        share = net / total if total else 0.0
        rate = n_needed / len(cut) if cut else 0.0
        out[lim] = {"cut": len(cut), "needed": n_needed, "saving": saving, "extra": extra, "net": net,
                    "net_saving_share": share, "needed_rate": rate, "needed_upper": n_upper,
                    "needed_upper_rate": n_upper / len(cut) if cut else 0.0,
                    "component_saving_share": saving / component if component else 0.0,
                    "passes": bool(cut) and share >= PASS_SAVING and rate <= PASS_NEEDED}
    passing = [lim for lim in limits if out[lim]["passes"]]
    best = max(passing, key=lambda lim: out[lim]["net"]) if passing else None
    return {"replay_code": REPLAY_CODE, "weighted_total": total, "component_total": component, "limits": out,
            "proposed": best}


def run(dirs, limits, today=None):
    """The screen on the sessions of the last PERIOD_DAYS days (dates of their first record, UTC)."""
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=PERIOD_DAYS)).isoformat()
    sessions = tb.load_sessions(dirs, since) if dirs else []
    return screen(sessions, sorted(limits))


def summary(result, value):
    """The two numbers a trial file and a record keep."""
    r = result["limits"][value]
    return {"replay_code": result["replay_code"], "net_saving_share": r["net_saving_share"],
            "needed_rate": r["needed_rate"], "passes": r["passes"]}


def render(result):
    lines = ["Replay bash_output_limit v{} on the last {} days: weighted cost ${:,.2f}, of it Bash and "
             "PowerShell results ${:,.2f}".format(result["replay_code"], PERIOD_DAYS, result["weighted_total"],
                                                  result["component_total"]),
             "  need %: a removed word named later and in no earlier record (lower bound, judged); "
             "upper %: version 1's rule, any removed word named later (upper bound, not judged)"]
    lines.append("  {:>7} {:>6} {:>7} {:>10} {:>10} {:>10} {:>7} {:>7} {:>7} {:>7}  pass".format(
        "limit", "cut", "needed", "saving", "extra", "net", "net %", "need %", "upper %", "comp %"))
    for lim, r in result["limits"].items():
        lines.append("  {:>7,} {:>6} {:>7} {:>10.4f} {:>10.4f} {:>10.4f} {:>6.2f}% {:>6.1f}% {:>6.1f}% {:>6.1f}%  {}"
                     .format(lim, r["cut"], r["needed"], r["saving"], r["extra"], r["net"],
                             100 * r["net_saving_share"], 100 * r["needed_rate"], 100 * r["needed_upper_rate"],
                             100 * r["component_saving_share"], "yes" if r["passes"] else "no"))
    lines.append("  proposed limit: {}".format(result["proposed"] if result["proposed"] else "none"))
    return "\n".join(lines)
