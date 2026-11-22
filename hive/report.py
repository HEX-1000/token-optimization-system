"""`hive report`: where the tokens of the Claude Code sessions of the last N days went (release A).

Read only. The logs are read through breakdown.load_sessions for the sessions whose first record is dated
in the last N days (UTC), as the replay does. Nothing is written, not even to the data folder, and
sessions.jsonl is not read. The tables are ports of print_category_table, print_compartments and
print_checks of the private research tool token_breakdown.py, which stays unchanged; the category table
leaves out a row whose four values are zero.

The default output is meant to be pasted into an issue, so it holds no project name, path, command,
MCP server name or session id: row labels come from the fixed list LABELS, every mcp:<server> row is
merged into one row "MCP tools", a label not on the list is printed as "other", and a model id is printed
only when it has the form of a Claude model id. --names adds the server names, the raw model ids and a
table per project, and says so in its first line. There is no table per day and none per session.

Prices come from the weights file WEIGHTS (api-v2: api-v1's five models and the other models of the
pricing page). weights.DEFAULT and the module-wide prices of breakdown are restored after a run.

The thinking and effort tables (step 13 of the dev repo's plan): thinking tokens come from the loader's calls;
the effort level does not, because breakdown.py does not read it, so call_fields reads each file's records a
second time. The thinking table splits the source table's thinking row by the model that produced each block:
the output, then the same blocks in the context of later calls (one cache write, then reads until the next
compaction). Its total is named as the most a lower level can remove at unchanged calls, never as a saving;
Checks counts the thinking drops inside a context, where the later context is too high. A log without the level or without a thinking token count works; such calls are counted under
"not logged" and under Checks. A level not on the list LEVELS is printed as "other level" without --names.
Standard library only.
"""
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from . import breakdown as tb
from . import profile, sense

WEIGHTS = "api-v2"
PERIOD_DAYS = 28
MCP = "MCP tools"
OTHER = "other"
OTHER_MODEL = "other model id"

# Row labels per category. Tool rows and prefix rows are the labels of breakdown.tool_group and
# breakdown.prefix_split; injected rows are the attachment types Claude Code wrote on the author's
# machine up to 2026-11-22 (and thinking_stripped, which breakdown.py names).
TOOL_ROWS = ("Read", "Grep/Glob", "Bash/PowerShell", "Edit/Write", "web", "Agent", "other")
LABELS = {
    "prefix": ("system text", "built-in tools", "other tools", "shape change", "unsplit (no shape logged)"),
    "injected": (
        "isMeta user", "agent_listing_delta", "auto_mode", "command_permissions", "compact_file_reference",
        "credential_org", "date", "deferred_tools_delta", "deferred_tools_record", "diagnostics",
        "dynamic_skill", "edited_text_file", "environment", "file", "hook_additional_context", "hook_success",
        "hook_system_message", "instructions", "invoked_skills", "mcp_instructions_delta", "model",
        "nested_memory", "plan_mode", "plan_mode_exit", "prompt_snapshot", "read_truncation_notice",
        "remote_session_change", "session_context", "silent_turn_reminder", "skill_listing", "task_status",
        "thinking_drop", "thinking_stripped", "total_tokens_reminder"),
    "prompt": ("typed or pasted", "queued while working"),
    "compaction summary": ("-",),
    "thinking": ("-",),
    "assistant text": ("-",),
    "tool input": TOOL_ROWS,
    "tool result": TOOL_ROWS,
    "image": ("from prompt", "from tool result"),
    "cache rebuild": ("model change", "shape change", "idle gap", "other"),
    "residual": ("unattributed",),
}
CAT_ORDER = ["prefix", "injected", "prompt", "compaction summary", "thinking", "assistant text",
             "tool input", "tool result", "image", "cache rebuild", "residual"]
KINDS = ("uncached input", "5-minute cache writes", "1-hour cache writes", "cache reads", "output")
MODEL_ID = re.compile(r"^(?:[a-z]{2,4}\.)?(?:anthropic\.)?claude-[a-z0-9.-]{1,60}(?:@\d{8})?(?::\d{1,3})?$")
LEVELS = ("low", "medium", "high", "xhigh", "max")      # Claude Code's effort levels, lowest first
NOT_LOGGED = "not logged"
OTHER_LEVEL = "other level"


def candidate_dirs():
    """The log folders hive sense reads by default, whether or not they exist (sense.default_dirs)."""
    home = Path.home()
    return [home / ".claude" / "projects", home / ".config" / "claude" / "projects"]


def label(cat, sub, names=False):
    if isinstance(sub, str) and sub.startswith("mcp:"):
        return sub if names else MCP
    return sub if sub in LABELS.get(cat, ()) else OTHER


def relabel(rows, names=False):
    out = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for (cat, sub), v in rows.items():
        row = out[(cat, label(cat, sub, names))]
        for i in range(4):
            row[i] += v[i]
    return dict(out)


def price_key(model):
    """The weights entry breakdown.price_for uses for a model id: exact, else the longest prefix; or None."""
    if not isinstance(model, str):
        return None
    if model in tb.PRICES:
        return model
    keys = [k for k in tb.PRICES if model.startswith(k)]
    return max(keys, key=len) if keys else None


def show_model(model, names=False):
    if not isinstance(model, str):
        return "(none)"
    return model if names or MODEL_ID.match(model) else OTHER_MODEL


def show_level(level, names=False):
    if level is None:
        return NOT_LOGGED
    if level in LEVELS:
        return level
    return str(level) if names else OTHER_LEVEL


def call_cost(call):
    """(tokens by kind, weighted cost by kind or None when the model has no price) of one call."""
    split = tb.call_split(call)
    rates = tb.call_rates(call)
    return split, (None if rates is None else [t * r for t, r in zip(split, rates)])


def call_fields(path):
    """Message id -> [effort level or None, True when the usage has a thinking token count], for one file.

    Records are taken as breakdown.parse_file takes them: a record with a string uuid not seen before in the
    file, synthetic messages left out, the message id or else the uuid as the key. The level is the record's
    effort, else its perTurnEffort (the two were equal wherever both were logged up to 2026-11-22); the first
    record of a message that has a level gives it."""
    out, seen = {}, set()
    for obj in tb.read_records(path):
        if obj is None or obj.get("type") not in ("user", "assistant", "attachment", "system"):
            continue
        uuid = obj.get("uuid")
        if not isinstance(uuid, str) or uuid in seen:
            continue
        seen.add(uuid)
        msg = obj.get("message")
        if obj["type"] != "assistant" or not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
            continue
        f = out.setdefault(msg.get("id") or uuid, [None, False])
        level = obj.get("effort")
        if level is None:
            level = obj.get("perTurnEffort")
        if f[0] is None:
            f[0] = level
        usage = msg.get("usage")
        det = usage.get("output_tokens_details") if isinstance(usage, dict) else None
        if isinstance(det, dict) and isinstance(det.get("thinking_tokens"), int):
            f[1] = True
    return out


def thinking_summary(sessions, names=False):
    """(by model, by model and level, calls without a thinking token count, thinking drops inside a context).

    By model: shown id -> [calls, thinking tokens, output, total, weights entries]; by level: (shown id,
    shown level) -> [calls, thinking tokens]. Calls, thinking tokens and output are over the counted calls:
    a call's thinking is its thinking tokens, at most its output tokens (as breakdown.size_outputs sizes the
    thinking item), and output is that at the output price of its model. Total is the weighted value of the
    thinking items (breakdown's it.w: the output, then the same block in the context of the later counted
    calls) by the model of the call that produced each item, so the totals add up to the thinking row of
    the source table. A thinking drop inside a context is a thinking_drop or thinking_stripped event
    whose next call does not start a context."""
    models = defaultdict(lambda: [0, 0, 0.0, 0.0, set()])
    levels = defaultdict(lambda: [0, 0])
    no_count = drops = 0
    for s in sessions:
        fields = call_fields(s.path)
        calls = s.fd.calls
        for c in calls:
            if not c.counted:
                continue
            level, has_count = fields.get(c.mid, (None, False))
            think = min(c.u["think"], c.u["out"])
            rates = tb.call_rates(c)
            name = show_model(c.model, names)
            m = models[name]
            m[0] += 1
            m[1] += think
            m[4].add(price_key(c.model))
            if rates is not None:
                m[2] += think * rates[4]
            row = levels[(name, show_level(level, names))]
            row[0] += 1
            row[1] += think
            no_count += 0 if has_count else 1
        for it in s.fd.items:
            if it.cat == "thinking":
                model = calls[it.out_call].model
                m = models[show_model(model, names)]
                m[3] += it.w
                m[4].add(price_key(model))
        steps = s.an.steps
        drops += sum(1 for pos, *_rest in s.fd.thinking_events
                     if pos < len(steps) and steps[pos].kind not in tb.FIRST_LIKE)
    return dict(models), dict(levels), no_count, drops


def summarize(sessions, names=False):
    """The numbers of every table, from analyzed sessions (prices as set in breakdown)."""
    d: dict = {"main": sum(1 for s in sessions if s.kind == "main"),
               "subagent": sum(1 for s in sessions if s.kind == "subagent"),
               "calls": sum(s.calls for s in sessions), "repeated": sum(s.inherited for s in sessions)}
    d["rows"] = relabel(tb.merge_rows(sessions), names)
    models = defaultdict(lambda: [0, 0.0, set()])        # shown id -> calls, weighted, weights entries
    kinds = [[0, 0.0] for _ in KINDS]
    unpriced = Counter()
    for s in sessions:
        for c in s.fd.calls:
            if not c.counted:
                continue
            split, cost = call_cost(c)
            m = models[show_model(c.model, names)]
            m[0] += 1
            m[2].add(price_key(c.model))
            for i, t in enumerate(split):
                kinds[i][0] += t
            if cost is None:
                unpriced[show_model(c.model, names)] += 1
                continue
            m[1] += sum(cost)
            for i, w in enumerate(cost):
                kinds[i][1] += w
    d["models"], d["kinds"], d["unpriced"] = dict(models), kinds, unpriced
    d["thinking"], d["levels"], d["no_think_count"], d["thinking_drops"] = thinking_summary(sessions, names)
    tops =[max((c.context for c in s.fd.calls if c.counted), default=0) for s in sessions
            if s.kind == "main" and s.calls]
    d["max_context_median"] = statistics.median(tops) if tops else None
    d["main_with_calls"] = len(tops)
    sums = {w: [0, 0] for w in profile.WINDOWS}
    for s in sessions:
        if sense.first_fields(s.path)[0] == sense.SDK_CLI:
            continue
        for w, (sum_c, added) in profile.window_sums(s).items():
            sums[w][0] += sum_c
            sums[w][1] += added
    d["s"] = {w: profile.s_value(*sums[w]) for w in profile.WINDOWS}
    ctx = sum(s.sum_context for s in sessions)
    d["residual_share"] = sum(s.residual_abs for s in sessions) / ctx if ctx else 0.0
    if names:
        groups = defaultdict(list)
        for s in sessions:
            groups[s.project_name].append(s)
        d["projects"] = {p: (len(g), sum(s.calls for s in g), relabel(tb.merge_rows(g), names))
                         for p, g in groups.items()}
    return d


# ------------------------------------------------------------------ text (ports of token_breakdown.py)

def fmt_n(x):
    return "{:,.0f}".format(x)


def fmt_usd(x):
    return "${:,.2f}".format(x)


def pct(x, total):
    return "{:5.1f}%".format(100.0 * x / total) if total else "    - "


def category_table(title, rows, subs=True):
    raw_total = sum(v[0] + v[1] + v[2] for v in rows.values())
    w_total = sum(v[3] for v in rows.values())
    out = [title, "  shares are of the total of all rows below (raw {} tokens, weighted {})".format(
        fmt_n(raw_total), fmt_usd(w_total)),
        "  {:<40}{:>15}{:>16}{:>13}{:>16}{:>7}{:>12}{:>7}".format(
            "category / row", "new", "carried", "output", "raw total", "raw %", "weighted", "w %")]

    def line(name, v):
        raw = v[0] + v[1] + v[2]
        out.append("  {:<40}{:>15}{:>16}{:>13}{:>16}{:>7}{:>12}{:>7}".format(
            name[:40], fmt_n(v[0]), fmt_n(v[1]), fmt_n(v[2]), fmt_n(raw), pct(raw, raw_total).strip(),
            fmt_usd(v[3]), pct(v[3], w_total).strip()))

    for cat in CAT_ORDER:
        cat_rows = {k[1]: v for k, v in rows.items() if k[0] == cat}
        if not cat_rows:
            continue
        line(cat, [sum(v[i] for v in cat_rows.values()) for i in range(4)])
        if subs and not (len(cat_rows) == 1 and "-" in cat_rows):
            for sub, v in sorted(cat_rows.items(), key=lambda kv: -kv[1][3]):
                if any(v):                      # a row of zeros adds nothing (not in the research tool)
                    line("   " + str(sub), v)
    line("TOTAL", [sum(v[i] for v in rows.values()) for i in range(4)])
    return out


def compartment_table(rows):
    comp = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for (cat, sub), v in rows.items():
        c = comp[tb.compartment(cat, sub)]
        for i in range(4):
            c[i] += v[i]
    raw_total = sum(v[0] + v[1] + v[2] for v in comp.values())
    w_total = sum(v[3] for v in comp.values())
    out = ["Compartments (grouping of the sources; no rule is applied here)",
           "  {:<50}{:>16}{:>7}{:>12}{:>7}".format("compartment", "raw total", "raw %", "weighted", "w %")]
    for name in sorted(comp):
        v = comp[name]
        raw = v[0] + v[1] + v[2]
        out.append("  {:<50}{:>16}{:>7}{:>12}{:>7}".format(name, fmt_n(raw), pct(raw, raw_total).strip(),
                                                          fmt_usd(v[3]), pct(v[3], w_total).strip()))
    return out


def model_order(models):
    """Shown model ids by weighted cost, then calls, then name (the order of every table by model)."""
    return sorted(models, key=lambda n: (-models[n][1], -models[n][0], n))


def model_table(models):
    w_total = sum(v[1] for v in models.values())
    out = ["By model (counted calls; the weights entry is the price row used)",
           "  {:<40}{:>10}{:>12}{:>7}   {}".format("model", "calls", "weighted", "w %", "weights entry")]
    for name in model_order(models):
        calls, w, keys = models[name]
        priced = any(k is not None for k in keys)
        entries = sorted(k for k in keys if k is not None) + (["no price"] if None in keys else [])
        out.append("  {:<40}{:>10,}{:>12}{:>7}   {}".format(
            name[:40], calls, fmt_usd(w) if priced else "-", pct(w, w_total).strip() if priced else "-",
            ", ".join(entries)))
    out.append("  {:<40}{:>10,}{:>12}".format("TOTAL", sum(v[0] for v in models.values()), fmt_usd(w_total)))
    return out


def kind_table(kinds):
    w_total = sum(w for _t, w in kinds)
    out = ["By token kind (tokens of all counted calls, cost of the calls with a price)",
           "  {:<26}{:>18}{:>12}{:>7}".format("kind", "tokens", "weighted", "w %")]
    for name, (t, w) in zip(KINDS, kinds):
        out.append("  {:<26}{:>18}{:>12}{:>7}".format(name, fmt_n(t), fmt_usd(w), pct(w, w_total).strip()))
    out.append("  {:<26}{:>18}{:>12}".format("TOTAL", fmt_n(sum(t for t, _w in kinds)), fmt_usd(w_total)))
    return out


def thinking_table(d):
    w_total = sum(v[1] for v in d["models"].values())
    rows = d["thinking"]
    head = "  {:<40}{:>10}{:>18}{:>12}{:>15}{:>12}{:>10}{:>9}"
    out = ["Thinking by model (counted calls; total = the thinking row of the source table)",
           head.format("model", "calls", "thinking tokens", "output", "later context", "total", "of model",
                       "of all")]
    # a model that only produced thinking in calls repeated from an earlier file comes after the others
    extra = sorted((n for n in rows if n not in d["models"]), key=lambda n: (-rows[n][3], n))
    for name in model_order(d["models"]) + extra:
        calls, tokens, output, total, keys = rows[name]
        w = d["models"][name][1] if name in d["models"] else 0.0
        priced = any(k is not None for k in keys)
        out.append(head.format(name[:40], "{:,}".format(calls), fmt_n(tokens),
                               *([fmt_usd(output), fmt_usd(total - output), fmt_usd(total), pct(total, w).strip(),
                                  pct(total, w_total).strip()] if priced else ["-"] * 5)))
    output, total = sum(v[2] for v in rows.values()), sum(v[3] for v in rows.values())
    out.append(head.format("TOTAL", "{:,}".format(sum(v[0] for v in rows.values())),
                           fmt_n(sum(v[1] for v in rows.values())), fmt_usd(output), fmt_usd(total - output),
                           fmt_usd(total), "", pct(total, w_total).strip()))
    out += ["  Output: thinking tokens at the output price. Later context: the same blocks in the calls after, one "
            "cache write and then cache reads until the next compaction or the end of the session.",
            "  The total is the most that a lower effort level can remove at unchanged calls: a thinking token that "
            "is not produced is not written to the cache and not read again. It is not a saving: a lower level can "
            "also change the answers and the number of prompts and calls, which this report does not measure.",
            "  Later context is too high where earlier thinking left the context before a compaction: after a "
            "thinking drop (Checks counts them) and on models whose earlier thinking the API removes."]
    return out


def level_rank(level):
    if level in LEVELS:
        return LEVELS.index(level)
    return len(LEVELS) + (1 if level == NOT_LOGGED else 0)


def level_table(d):
    order = {name: i for i, name in enumerate(model_order(d["models"]))}
    head = "  {:<40}{:<14}{:>10}{:>26}"
    out = ["Effort levels in the logs (counted calls; the level Claude Code wrote into each call's record)",
           head.format("model", "level", "calls", "thinking tokens per call")]
    for (name, level), (calls, tokens) in sorted(
            d["levels"].items(), key=lambda kv: (order.get(kv[0][0], len(order)), level_rank(kv[0][1]), kv[0])):
        out.append(head.format(name[:40], level[:14], "{:,}".format(calls), fmt_n(tokens / calls)))
    out.append("  The levels were not given to the calls at random: a level is chosen with the work, so the "
               "difference between two levels describes these calls and is not the effect of a level.")
    return out


def context_lines(d):
    med = d["max_context_median"]
    out = ["Context",
           "  Median of the largest context per main session: {} ({} main sessions with a counted call)".format(
               fmt_n(med) + " tokens" if med is not None else "-", d["main_with_calls"])]
    out.append("  " + "; ".join("S at {:,}: {:.1f}%".format(w, 100 * d["s"][w]) for w in profile.WINDOWS))
    out.append("  S at W: the share of the context tokens read that a compaction whenever the context passes W "
               "would remove, each compaction leaving {:,} tokens. It leaves out the summary's cost and what is "
               "read again after a compaction, so a real saving is smaller. Files with entrypoint {} are left "
               "out, as in hive sense.".format(profile.AFTER, sense.SDK_CLI))
    return out


def check_lines(d):
    n = sum(d["unpriced"].values())
    ids = ", ".join("{} {:,}".format(m, k) for m, k in d["unpriced"].most_common())
    return ["Checks",
            "  Calls without a price: {:,}{}".format(n, " ({})".format(ids) if n else ""),
            "  Every weighted total above leaves out the calls without a price.",
            "  Calls without a thinking token count in the log: {:,}; their thinking is counted as 0.".format(
                d["no_think_count"]),
            "  Thinking drops inside a context (thinking_drop or thinking_stripped events not at a call that "
            "starts a context): {:,}".format(d["thinking_drops"]),
            "  Residual (context no source took): {:.2f}% of the summed context".format(100 * d["residual_share"])]


def render(d, since, today, days, names=False):
    out = []
    if names:
        out += ["This output holds local names (projects, MCP servers, model ids as logged). Check it before "
                "you post it.", ""]
    out += ["Hive {} report, read only".format(__version__),
            "Period: sessions whose first record is dated {} to {} (last {} days, UTC)".format(since, today, days),
            "Sessions: {} main, {} subagent; counted calls {:,} (calls repeated from an earlier file, not counted: "
            "{:,})".format(d["main"], d["subagent"], d["calls"], d["repeated"]),
            "Weights: {}. Weighted cost is each token times the API price of its billing type for the call's model; "
            "it is not a bill, and on a subscription it stands in for plan usage.".format(WEIGHTS), ""]
    out += category_table("By source: tokens by category (main and subagent sessions)", d["rows"])
    out += [""] + compartment_table(d["rows"])
    out += [""] + model_table(d["models"])
    out += [""] + kind_table(d["kinds"])
    out += [""] + thinking_table(d)
    out += [""] + level_table(d)
    out += [""] + context_lines(d)
    out += [""] + check_lines(d)
    if names:
        for name, (n, calls, rows) in sorted(d["projects"].items(),
                                             key=lambda kv: (-sum(v[3] for v in kv[1][2].values()), kv[0])):
            out += [""] + category_table("Project {} ({} sessions, {:,} calls)".format(name, n, calls), rows,
                                         subs=False)
    return "\n".join(out)


def run(dirs=None, days=PERIOD_DAYS, names=False, today=None):
    """(text, exit code). dirs None means the default log folders."""
    searched = [Path(x) for x in dirs] if dirs is not None else candidate_dirs()
    found = [x for x in searched if x.is_dir()]
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    folders = ", ".join(str(x) for x in searched) or "(none)"
    if not any(True for _ in tb.find_logs(found)):
        return ("No Claude Code session log found. Folders searched: {}. Pass the log folder with "
                "--dir.".format(folders), 1)
    prev = tb.WEIGHTS
    tb.use_weights(WEIGHTS)
    try:
        sessions = tb.load_sessions(found, since)
        if not sessions:
            return ("No Claude Code session with a model call started on {} or later. Folders searched: {}."
                    .format(since, folders), 1)
        return render(summarize(sessions, names), since, today.isoformat(), days, names), 0
    finally:
        tb.use_weights(prev)
