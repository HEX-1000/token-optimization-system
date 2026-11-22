"""Trial records: builder, validator, `hive record`, `hive share` (plan section 6).

A record has no free text field. Every value is a number, an enum from a fixed list, a catalog or
weights id (^[a-z0-9-]{1,40}$), a version (^\\d+(\\.\\d+){0,3}$) or an ISO week (^\\d{4}-W\\d{2}$), each
at a fixed position of the schema below; keys are fixed too, except the names of a method's parameters,
which are ids. Numbers have at most 3 significant digits; node shares are multiples of 0.05. The
validator rejects anything else, so a record can carry no name, path, text or fine date.
"""
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from . import __version__, guards, store, verdict

SCHEMA_ID = "hive-record/1"
ID = re.compile(r"^[a-z0-9-]{1,40}$")
VERSION = re.compile(r"^\d+(\.\d+){0,3}$")
WEEK = re.compile(r"^\d{4}-W\d{2}$")
OS_VALUES = ("windows", "linux", "macos", "other")
PLANS = ("pro", "max5", "max20", "team", "api", "unknown")
FLAGS = ("settings_changed", "overridden", "stopped_by_user_harm", "stopped_by_user_other", "max_calendar_days",
         "excluded_sessions_over_10pct", "cost_undefined_draws", "guard_undefined_draws", "not_applied")
SHARES = ("agentic", "model_effort", "chat", "vision", "other")
SHARE_PERIOD_DAYS = 28

NUM, INT, SHARE = "num", "int", "share"
ARM = {"active_days": INT, "prompts": INT, "cost_per_prompt": ("nullable", NUM),
       "calls_per_prompt": ("nullable", NUM), "guards": {g: ("nullable", NUM) for g in guards.NAMES}}
INTERVAL = {"estimate": ("nullable", NUM), "low": ("nullable", NUM), "high": ("nullable", NUM)}
SCHEMA = {
    "schema": ("const", SCHEMA_ID),
    "hive": "version",
    "verdict_code": "version",
    "weights": "id",
    "method": {"id": "id", "version": INT, "params": ("map", "id", NUM)},
    "kept_on": ("list", "id"),
    "client": {"name": ("enum", ("claude-code",)), "version": ("nullable", "version")},
    "os": ("enum", OS_VALUES),
    "plan": ("enum", PLANS),
    "start_week": "week",
    "node_shares": {k: SHARE for k in SHARES},
    "arms": {"on": ARM, "off": ARM},
    "cost_ratio": INTERVAL,
    "own_guard_ratio": INTERVAL,
    "component_ratio": ("nullable", INTERVAL),
    "cost_per_call_ratio": ("nullable", INTERVAL),
    "notes": {"on": INT, "off": INT},
    "replay": ("nullable", {"net_saving_share": NUM, "needed_rate": NUM}),
    "verdict": ("enum", verdict.VERDICTS),
    "flags": ("list", ("enum", FLAGS)),
}


# ------------------------------------------------------------------ rounding

def sig3(x):
    """x with at most 3 significant digits; an int stays an int."""
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, int):
        if abs(x) < 1000:
            return x
        digits = len(str(abs(x)))
        return int(round(x, 3 - digits))
    if not math.isfinite(x) or x == 0:
        return 0.0 if x == 0 else x
    return float("{:.3g}".format(x))


def share05(x):
    return round(round(x / 0.05) * 0.05, 2)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ------------------------------------------------------------------ validator

def _check(v, spec, where, errors):
    if isinstance(spec, dict):
        if not isinstance(v, dict):
            errors.append(where + ": not an object")
            return
        extra = sorted(set(v) - set(spec))
        missing = sorted(set(spec) - set(v))
        if extra:
            errors.append(where + ": keys not in the schema ({} of them)".format(len(extra)))
        if missing:
            errors.append(where + ": missing keys " + ", ".join(missing))
        for k, s in spec.items():
            if k in v:
                _check(v[k], s, where + "." + k, errors)
        return
    if isinstance(spec, tuple):
        kind = spec[0]
        if kind == "nullable":
            if v is not None:
                _check(v, spec[1], where, errors)
        elif kind == "const":
            if v != spec[1]:
                errors.append(where + ": not " + spec[1])
        elif kind == "enum":
            if not isinstance(v, str) or v not in spec[1]:
                errors.append(where + ": not one of the fixed values")
        elif kind == "list":
            if not isinstance(v, list):
                errors.append(where + ": not a list")
            else:
                for i, x in enumerate(v):
                    _check(x, spec[1], "{}[{}]".format(where, i), errors)
        elif kind == "map":
            if not isinstance(v, dict) or not v:
                errors.append(where + ": not a non-empty object")
            else:
                for k, x in v.items():
                    _check(k, spec[1], where + " key", errors)
                    _check(x, spec[2], where + " value", errors)
        return
    if spec == "id":
        ok = isinstance(v, str) and ID.match(v)
    elif spec == "version":
        ok = isinstance(v, str) and VERSION.match(v)
    elif spec == "week":
        ok = isinstance(v, str) and WEEK.match(v)
    elif spec == NUM:
        ok = is_number(v) and math.isfinite(v) and sig3(v) == v
    elif spec == INT:
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0 and sig3(v) == v
    elif spec == SHARE:
        ok = is_number(v) and 0 <= v <= 1 and share05(v) == v
    else:
        raise AssertionError(spec)
    if not ok:
        errors.append(where + ": not a valid " + spec)


def validate(rec):
    """List of problems; an empty list means the record is valid. Problems never quote a value.

    After the schema, ids are checked against this release: the method and kept methods must be catalog
    entries, the parameter must be the method's own with an allowed value, and the weights id must be a
    weights file. So no position takes a string that is not a known value."""
    from . import catalog, weights
    errors = []
    _check(rec, SCHEMA, "record", errors)
    if errors:
        return errors
    ids = catalog.all_ids()
    m = rec["method"]
    if m["id"] not in ids:
        errors.append("record.method.id: not a catalog entry of this release")
    else:
        entry = catalog.load(m["id"])
        name, allowed = catalog.param(entry)
        if not 1 <= m["version"] <= entry["version"]:
            errors.append("record.method.version: not a version of the catalog entry")
        if set(m["params"]) != {name} or m["params"][name] not in allowed:
            errors.append("record.method.params: not the entry's parameter with an allowed value")
    if any(k not in ids for k in rec["kept_on"]):
        errors.append("record.kept_on: not a catalog entry of this release")
    if not (weights.WEIGHTS_DIR / (rec["weights"] + ".json")).is_file():
        errors.append("record.weights: not a weights file of this release")
    return errors


def validate_bytes(data):
    try:
        rec = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (ValueError, UnicodeDecodeError):
        return ["record: not JSON"]
    return validate(rec)


# ------------------------------------------------------------------ builder

def os_name(platform=None):
    p = platform or sys.platform
    if p.startswith("win"):
        return "windows"
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    return "other"


def iso_week(d):
    y, w, _ = d.isocalendar()
    return "{}-W{:02d}".format(y, w)


def node_shares(rows, start_date):
    """Shares of weighted cost by compartment over the sessions of the SHARE_PERIOD_DAYS days before the start."""
    lo = (start_date - timedelta(days=SHARE_PERIOD_DAYS)).isoformat()
    hi = start_date.isoformat()
    total = Counter()
    for r in rows:
        day = (r.get("start") or "")[:10]
        if lo <= day < hi:
            c = r["cost_by_compartment"]
            total["agentic"] += c["agentic"]
            total["model_effort"] += c["model_effort"]
            total["chat"] += c["chat"]
            total["vision"] += c["vision"]
            total["other"] += c["prefix"] + c["rebuild_other"] + c["residual"]
    s = sum(total.values())
    return {k: share05(total[k] / s) if s else 0.0 for k in SHARES}


def _num(x):
    return None if x is None else sig3(x)


def build(trial, rows, kept=(), plan="unknown", platform=None):
    """The record dict of a finished trial. Raises ValueError when no record may be made."""
    res = trial.get("result") or {}
    if res.get("verdict") is None:
        raise ValueError("no record: the trial has no verdict ({})".format(res.get("error", "not finished")))
    start = date.fromisoformat(trial["start_date"])
    versions = Counter(r.get("client_version") for r in rows
                       if (r.get("start") or "")[:10] >= trial["start_date"] and r.get("entrypoint") == "cli")
    client_version = next((v for v, _ in versions.most_common() if isinstance(v, str) and VERSION.match(v)), None)
    arms = {}
    for arm in ("on", "off"):
        a = res["arms"][arm]
        arms[arm] = {"active_days": sig3(a["active_days"]), "prompts": sig3(a["prompts"]),
                     "cost_per_prompt": _num(a["cost_per_prompt"]), "calls_per_prompt": _num(a["calls_per_prompt"]),
                     "guards": {g: _num(a["guards"].get(g)) for g in guards.NAMES}}
    rp = trial.get("replay")
    rec = {
        "schema": SCHEMA_ID,
        "hive": __version__,
        "verdict_code": trial["verdict_code"],
        "weights": trial["weights"],
        "method": {"id": trial["method"]["id"], "version": trial["method"]["version"],
                   "params": {k: sig3(v) for k, v in trial["method"]["params"].items()}},
        "kept_on": sorted(k["id"] for k in kept if k["id"] != trial["method"]["id"]),
        "client": {"name": "claude-code", "version": client_version},
        "os": os_name(platform),
        "plan": plan if plan in PLANS else "unknown",
        "start_week": iso_week(start),
        "node_shares": node_shares(rows, start),
        "arms": arms,
        "cost_ratio": {k: _num(v) for k, v in res["cost_ratio"].items()},
        "own_guard_ratio": {k: _num(v) for k, v in res["own_guard_ratio"].items()},
        "component_ratio": None if res.get("component_ratio") is None else
        {k: _num(v) for k, v in res["component_ratio"].items()},
        "cost_per_call_ratio": None if res.get("cost_per_call_ratio") is None else
        {k: _num(v) for k, v in res["cost_per_call_ratio"].items()},
        "notes": {"on": sig3(res["notes"]["on"]), "off": sig3(res["notes"]["off"])},
        "replay": None if not rp else {"net_saving_share": sig3(rp["net_saving_share"]),
                                       "needed_rate": sig3(rp["needed_rate"])},
        "verdict": res["verdict"],
        "flags": [f for f in FLAGS if f in res.get("flags", [])],
    }
    problems = validate(rec)
    if problems:
        raise ValueError("the record does not pass the validator: " + "; ".join(problems))
    return rec


def to_bytes(rec):
    return (json.dumps(rec, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def write(rec, rand=os.urandom):
    d = store.data_dir() / "records"
    d.mkdir(parents=True, exist_ok=True)
    path = d / (rand(8).hex() + ".json")
    store.write_bytes_atomic(path, to_bytes(rec))
    return path


def share_text(path):
    data = Path(path).read_bytes()
    problems = validate_bytes(data)
    head = ["Record file: {}".format(path)]
    if problems:
        return "\n".join(head + ["This record does not pass the validator and must not be posted:"] +
                         ["  " + p for p in problems]), False
    return "\n".join(head + [
        "Its exact bytes follow between the lines. The program sends nothing.",
        "To share it, open a new issue with the form \"Trial record\" in the public repository and paste them.",
        "An issue is public and shows your GitHub user name next to the record.",
        "-" * 72, data.decode("ascii").rstrip("\n"), "-" * 72]), True
