"""The method catalog: one JSON file per method in catalog/ (plan section 3, item 4).

The first install supports kind "setting" only: a documented Claude Code setting in the user settings
file, which costs no tokens when it is off and reverts exactly.

Two fields serve the record and not the verdict:
- component: the sensor metric the method acts on (COMPONENTS), or null. The record reports its ratio on
  over off per typed prompt, with an interval; the verdict does not use it.
- applied_check: a session metric (APPLIED_METRICS). An on-period session whose value is above the
  method's parameter shows that the setting did not apply, and the record gets the flag not_applied.
"""
import json
import re
from pathlib import Path

CATALOG_DIR = Path(__file__).resolve().parent / "catalog"
ID = re.compile(r"^[a-z0-9-]{1,40}$")
KINDS = ("setting",)
METRICS = ("s_window", "bash_cost_share")
# component id -> weighted cost of that component in a sessions.jsonl row
COMPONENTS = {"bash_cost": lambda row: row["bash"]["cost"]}
# applied-check metric -> its value in a sessions.jsonl row (0 for a row written before the field existed)
APPLIED_METRICS = {
    "shell_ok_max_chars": lambda row: row.get("shell_ok_max_chars", 0),   # longest successful shell result
    "max_context": lambda row: row.get("max_context", 0),                 # largest context of a main call
}
REQUIRED = ("id", "version", "compartment", "kind", "setting", "params", "overridden_by", "randomization_unit",
            "replay", "own_guard", "component", "applied_check", "eligibility", "min_active_days_per_arm",
            "max_calendar_days", "source")


def load(method_id):
    if not ID.match(method_id or ""):
        raise ValueError("method id must match ^[a-z0-9-]{1,40}$")
    path = CATALOG_DIR / (method_id + ".json")
    if not path.is_file():
        raise ValueError("no catalog entry " + method_id)
    m = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED if k not in m]
    if missing or m["id"] != method_id or m["kind"] not in KINDS or m["eligibility"]["metric"] not in METRICS             or (m["component"] is not None and m["component"] not in COMPONENTS)             or not isinstance(m["applied_check"], dict) or m["applied_check"].get("metric") not in APPLIED_METRICS:
        raise ValueError("catalog entry {} is malformed (missing {})".format(method_id, missing))
    if len(m["params"]) != 1:
        raise ValueError("catalog entry {}: exactly one parameter is supported".format(method_id))
    return m


def all_ids():
    return sorted(p.stem for p in CATALOG_DIR.glob("*.json"))


def param(m):
    """(name, allowed values) of the method's single parameter."""
    (name, values), = m["params"].items()
    return name, values


def component_cost(m, row):
    """Weighted cost of the method's component in one sessions.jsonl row; None when it has none."""
    return None if m["component"] is None else COMPONENTS[m["component"]](row)


def not_applied(m, value, row):
    """True when an on-period session's row shows that the setting with this value did not apply."""
    return APPLIED_METRICS[m["applied_check"]["metric"]](row) > value


def on_value_text(m, value):
    """JSON text written to the settings file for the on state."""
    if m["setting"]["type"] == "string":
        return json.dumps(str(value))
    return json.dumps(int(value))
