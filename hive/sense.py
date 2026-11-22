"""The sensor (`hive sense`): one row per session in sessions.jsonl (plan section 3, item 1).

A row holds the session id and project (local names that never reach a record) and numbers: start
and end, client version, entrypoint, counted calls per model, typed prompts, raw tokens, weighted
cost by compartment, the sums behind S at the windows of hive.profile, the Bash result carry, guard
counts, format facts, and two applied-check facts: the longest successful Bash or PowerShell result in
characters as logged (`shell_ok_max_chars`; Claude Code logs a result it cut as a short
<persisted-output> preview) and the largest context of a counted main-file call (`max_context`). Subagent files count with their parent session. Rows whose log was deleted
are kept. A row is rebuilt only when one of its files changed since it was written (or the weights or
ROW_VERSION changed), so a session whose earlier file was deleted keeps the numbers it had while that file
existed.
"""
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import breakdown as tb
from . import guards, profile, prompts, store

ROW_VERSION = 2
SDK_CLI = "sdk-cli"
TOKEN = re.compile(r"^[A-Za-z0-9._:<>+-]{1,64}$")
COMPARTMENTS = {
    "agentic work": "agentic",
    "model and effort choice": "model_effort",
    "chat text": "chat",
    "vision input": "vision",
    "prefix (fixed cost, not a candidate)": "prefix",
    "cache rebuild, other causes (not a compartment)": "rebuild_other",
    "residual (no source)": "residual",
}


def default_dirs():
    home = Path.home()
    return [d for d in (home / ".claude" / "projects", home / ".config" / "claude" / "projects") if d.is_dir()]


def token(value):
    """A log value kept in a row only when it looks like an identifier (model id, version, entrypoint)."""
    if value is None:
        return None
    return value if isinstance(value, str) and TOKEN.match(value) else "other"


def first_fields(path):
    """(first entrypoint value, first version value) of a log file, top-level keys only."""
    entry = version = None
    with open(path, "rb") as fh:
        for line in fh:
            need_e = entry is None and b'"entrypoint"' in line
            need_v = version is None and b'"version"' in line
            if not (need_e or need_v):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if entry is None and isinstance(obj.get("entrypoint"), str) and obj["entrypoint"]:
                entry = obj["entrypoint"]
            if version is None and isinstance(obj.get("version"), str) and obj["version"]:
                version = obj["version"]
            if entry is not None and version is not None:
                break
    return entry, version


def session_id(root, path):
    """Id of the main session a file belongs to: its own stem, or for <session>/subagents/<file> the
    session folder's name."""
    parts = Path(path).relative_to(root).parts
    if "subagents" in parts:
        i = parts.index("subagents")
        if i > 0:
            return parts[i - 1]
    return Path(path).stem


def find_root(dirs, path):
    for d in dirs:
        try:
            Path(path).relative_to(d)
            return d
        except ValueError:
            continue
    return None


def last_ts(fd):
    ts = [r.ts for r in fd.recs if r.ts]
    return max(ts) if ts else fd.first_ts


def signature(files):
    """[size, mtime] of each file in path order: enough to see a change, and no names."""
    out = []
    for p in sorted(str(f) for f in files):
        try:
            st = os.stat(p)
            out.append([st.st_size, int(st.st_mtime)])
        except OSError:
            out.append([None, None])
    return out


def build_row(sid, main, subs, fold_case, now):
    files = ([main] if main is not None else []) + subs
    first = main if main is not None else min(subs, key=lambda s: s.fd.first_ts or "")
    entry, version = first_fields(first.path)
    row = {"row": ROW_VERSION, "session": sid, "project": first.fd.project,
           "has_main": main is not None, "subagent_files": len(subs),
           "start": first.fd.first_ts, "end": last_ts(first.fd),
           "client_version": token(version), "entrypoint": token(entry),
           "weights": tb.WEIGHTS, "guards_version": guards.GUARDS_VERSION,
           "sensed": now, "log_present": True}
    by_model = Counter()
    for s in files:
        for c in s.fd.calls:
            if c.counted:
                by_model[token(c.model) or "none"] += 1
    row["calls_by_model"] = dict(by_model)
    row["calls"] = sum(s.calls for s in files)
    row["main_calls"] = main.calls if main is not None else 0
    row["tokens"] = {k: sum(s.totals[k] for s in files) for k in ("input", "cw", "cr", "out")}
    row["tokens_main_file"] = dict(main.all_totals) if main is not None else None
    comp = defaultdict(float)
    for s in files:
        for (cat, sub), v in s.rows.items():
            comp[COMPARTMENTS[tb.compartment(cat, sub)]] += v[3]
    row["cost_by_compartment"] = {k: comp.get(k, 0.0) for k in COMPARTMENTS.values()}
    row["cost"] = sum(comp.values())
    s_sums = {str(w): [0, 0] for w in profile.WINDOWS}
    for s in files:
        if first_fields(s.path)[0] == SDK_CLI:
            continue
        for w, (sum_c, added) in profile.window_sums(s).items():
            s_sums[str(w)][0] += sum_c
            s_sums[str(w)][1] += added
    row["s_sums"] = s_sums
    bash = [it for s in files for it in s.fd.items if it.cat == "tool result" and it.tool in guards.SHELL]
    row["bash"] = {"results": len(bash), "new": sum(it.new for it in bash),
                   "carried": sum(it.carried for it in bash), "cost": sum(it.w for it in bash)}
    row["shell_ok_max_chars"] = max((it.chars for it in bash if not it.is_error), default=0)
    row["max_context"] = max((c.context for c in main.fd.calls if c.counted), default=0) if main is not None else 0
    g, ids, missing = {}, 0, Counter()
    for s in files:
        mids = {c.mid for c in s.fd.calls if c.counted}
        part, facts = guards.file_guards(s.path, mids, fold_case)
        guards.add(g, part)
        ids += facts["ids"]
        missing.update(facts["missing"])
    if main is not None:
        n_prompts, skipped, _times = prompts.count_prompts(main.path)
    else:
        n_prompts, skipped = 0, Counter()
    g["interrupt"] = [skipped.get("interrupt", 0), n_prompts]
    row["guards"] = {name: g.get(name, [0, 0]) for name in guards.NAMES}
    row["prompts"] = n_prompts
    row["not_prompts"] = {r: skipped.get(r, 0) for r in prompts.REASONS}
    row["checks"] = {"usage_ids": ids, "missing": dict(missing),
                     "restarts": sum(s.kinds["restart"] for s in files),
                     "unpriced_calls": sum(s.unpriced for s in files),
                     "residual_abs": sum(s.residual_abs for s in files),
                     "sum_context": sum(s.sum_context for s in files),
                     "synthetic": sum(s.synthetic for s in files)}
    row["files"] = signature([s.path for s in files])
    return row


def sense(dirs=None, fold_case=None, now=None):
    """Read the logs, update sessions.jsonl, return counts (no names)."""
    dirs = [Path(d) for d in (dirs or default_dirs())]
    fold_case = (os.name == "nt") if fold_case is None else fold_case
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sessions = tb.load_sessions(dirs) if dirs else []
    groups = defaultdict(lambda: [None, []])
    for s in sessions:
        root = find_root(dirs, s.path)
        sid = session_id(root, s.path) if root is not None else s.path.stem
        if s.kind == "main":
            groups[sid][0] = s
        else:
            groups[sid][1].append(s)
    path = store.data_dir() / "sessions.jsonl"
    old = {r["session"]: r for r in store.read_jsonl(path)}
    out, built, kept_same = {}, 0, 0
    for sid, (main, subs) in groups.items():
        files = [s.path for s in ([main] if main is not None else []) + subs]
        prev = old.get(sid)
        if prev is not None and prev.get("files") == signature(files) and prev.get("weights") == tb.WEIGHTS                 and prev.get("row") == ROW_VERSION:
            prev["log_present"] = True
            out[sid] = prev
            kept_same += 1
            continue
        out[sid] = build_row(sid, main, subs, fold_case, now)
        built += 1
    deleted = 0
    for sid, r in old.items():
        if sid not in out:
            r["log_present"] = False
            out[sid] = r
            deleted += 1
    rows = sorted(out.values(), key=lambda r: (r.get("start") or "", r["session"]))
    store.write_jsonl(path, rows)
    return {"sessions_on_disk": len(groups), "rows_built": built, "rows_unchanged": kept_same,
            "rows_of_deleted_logs": deleted, "rows": len(rows)}
