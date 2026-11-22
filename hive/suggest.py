"""What to try next (`hive suggest`, plan section 3, item 8). The user chooses; nothing is chosen here.

For each catalog method and parameter value: the node's eligibility metric over the last 28 days, the
replay estimate where the method has a replay, and the hive's results for the method from the records
shipped with the release (results/records/ next to this package): records by verdict and the median
cost ratio.
"""
import json
import statistics
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from . import catalog, profile, record, replay

PERIOD_DAYS = 28
RECORDS_DIR = Path(__file__).resolve().parent.parent / "results" / "records"


def recent(rows, today, days=PERIOD_DAYS):
    lo = (today - timedelta(days=days)).isoformat()
    return [r for r in rows if (r.get("start") or "")[:10] >= lo]


def metrics(rows, today):
    rs = recent(rows, today)
    s = {}
    for w in profile.WINDOWS:
        sum_c = sum(r["s_sums"][str(w)][0] for r in rs)
        added = sum(r["s_sums"][str(w)][1] for r in rs)
        s[str(w)] = profile.s_value(sum_c, added) if sum_c else None
    cost = sum(r["cost"] for r in rs)
    bash = sum(r["bash"]["cost"] for r in rs)
    latest = max((r for r in rs if version_tuple(r.get("client_version"))), key=lambda r: r.get("start") or "",
                 default=None)
    return {"sessions": len(rs), "s_window": s, "bash_cost_share": bash / cost if cost else None, "cost": cost,
            "client_version": latest["client_version"] if latest else None}


def version_tuple(v):
    """(2, 1, 322) from "2.1.322"; None when v is not a version."""
    if not isinstance(v, str) or not record.VERSION.match(v):
        return None
    return tuple(int(x) for x in v.split("."))


def eligibility(m, met, value):
    """(metric value, minimum) for one parameter value of a method. The metric value is None, so the
    method is not eligible, when the newest session's client is older than the entry's min_client_version
    (an older client ignores the setting)."""
    need = version_tuple(m.get("min_client_version"))
    have = version_tuple(met.get("client_version"))
    e = m["eligibility"]
    if need and (have is None or have < need):
        return None, e["min"]
    if e["metric"] == "s_window":
        return met["s_window"].get(str(value)), e["min"]
    if e["metric"] == "bash_cost_share":
        return met["bash_cost_share"], e["min"]
    raise ValueError(e["metric"])


def hive_results(method_id, records_dir=RECORDS_DIR):
    verdicts, ratios = Counter(), []
    for f in sorted(Path(records_dir).glob("*.json")) if Path(records_dir).is_dir() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if record.validate(rec) or rec["method"]["id"] != method_id:
            continue
        verdicts[rec["verdict"]] += 1
        if rec["cost_ratio"]["estimate"] is not None:
            ratios.append(rec["cost_ratio"]["estimate"])
    return {"records": sum(verdicts.values()), "by_verdict": dict(verdicts),
            "median_cost_ratio": statistics.median(ratios) if ratios else None}


def suggest(rows, dirs, today=None, run_replay=True, records_dir=RECORDS_DIR):
    today = today or date.today()
    met = metrics(rows, today)
    out = []
    for mid in catalog.all_ids():
        m = catalog.load(mid)
        name, values = catalog.param(m)
        rp = replay.run(dirs, values, today) if (run_replay and m["replay"] == "bash_output_limit") else None
        for v in values:
            val, lo = eligibility(m, met, v)
            out.append({"method": mid, "param": name, "value": v, "metric": m["eligibility"]["metric"],
                        "metric_value": val, "min": lo, "eligible": val is not None and val >= lo,
                        "replay": replay.summary(rp, v) if rp else None,
                        "proposed_by_replay": rp is not None and rp["proposed"] == v,
                        "hive": hive_results(mid, records_dir)})
    return met, out


def render(met, out):
    lines = ["Sessions in the last {} days: {}; weighted cost ${:,.2f}".format(PERIOD_DAYS, met["sessions"], met["cost"])]
    for o in out:
        val = "-" if o["metric_value"] is None else "{:.1%}".format(o["metric_value"])
        rp = o["replay"]
        rtext = "no replay" if rp is None else "replay net {:.2%}, needed {:.0%}, {}".format(
            rp["net_saving_share"], rp["needed_rate"], "passes" if rp["passes"] else "does not pass")
        h = o["hive"]
        htext = "hive: no records" if not h["records"] else "hive: {} records ({}), median cost ratio {}".format(
            h["records"], ", ".join("{} {}".format(k, n) for k, n in sorted(h["by_verdict"].items())),
            "-" if h["median_cost_ratio"] is None else "{:.3g}".format(h["median_cost_ratio"]))
        lines.append("{} {}={}: {} {} (min {:.0%}) -> {}; {}{}; {}".format(
            o["method"], o["param"], o["value"], o["metric"], val, o["min"],
            "eligible" if o["eligible"] else "not eligible", rtext,
            " (proposed)" if o["proposed_by_replay"] else "", htext))
    return "\n".join(lines)
