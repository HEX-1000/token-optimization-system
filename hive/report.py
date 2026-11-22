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


def call_cost(call):
    """(tokens by kind, weighted cost by kind or None when the model has no price) of one call."""
    split = tb.call_split(call)
    rates = tb.call_rates(call)
    return split, (None if rates is None else [t * r for t, r in zip(split, rates)])


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
    tops = [max((c.context for c in s.fd.calls if c.counted), default=0) for s in sessions
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


def model_table(models):
    w_total = sum(v[1] for v in models.values())
    out = ["By model (counted calls; the weights entry is the price row used)",
           "  {:<40}{:>10}{:>12}{:>7}   {}".format("model", "calls", "weighted", "w %", "weights entry")]
    for name, (calls, w, keys) in sorted(models.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0])):
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
