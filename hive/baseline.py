"""The node's baseline (`hive baseline`, plan step 5): numbers a trial plan needs before a trial starts.

Over the last PERIOD_DAYS complete trial days (04:00 to 04:00 local time, hive.schedule), from the sessions
a trial counts (hive.trial.counted_session):

- active days: trial days with at least one counted session that stays inside its trial day;
- the 04:00 boundary: counted sessions whose last record falls in a later trial day than their first,
  and their share of the cost (a trial excludes such a session when a tick falls inside it);
- per-day spread: for each active day, weighted cost per typed prompt and the component's cost per typed
  prompt (sum over the day / prompts of the day), with median and the standard deviation of the log;
- guards: events over opportunities summed over the counted sessions of the period.

The day pool (one aggregate per active day) is what `hive simulate power --own` draws from. Only numbers
leave this module; no session id or project name is printed.
"""
import math
import statistics
from datetime import timedelta

from . import catalog, guards, schedule
from .trial import counted_session

PERIOD_DAYS = 28


def period(today=None, tz=None):
    """(first, last) trial dates of the last PERIOD_DAYS complete trial days before `today`'s trial day."""
    if today is None:
        from datetime import datetime, timezone
        today = schedule.trial_date(datetime.now(timezone.utc), tz)
    return today - timedelta(days=PERIOD_DAYS), today - timedelta(days=1)


def spans(r, tz=None):
    return schedule.trial_date(r["end"], tz) != schedule.trial_date(r["start"], tz)


def day_pool(rows, m, tz=None, today=None):
    """{trial date: day aggregate} of the active days of the period, from counted sessions that stay
    inside their trial day. A day aggregate has cost, prompts, calls, component (weighted cost of the
    method's component, 0.0 when it has none), sessions and guards {name: [events, opportunities]}."""
    first, last = period(today, tz)
    out = {}
    for r in rows:
        if not counted_session(r) or spans(r, tz):
            continue
        d = schedule.trial_date(r["start"], tz)
        if not first <= d <= last:
            continue
        a = out.setdefault(d, {"cost": 0.0, "prompts": 0, "calls": 0, "component": 0.0, "sessions": 0,
                               "guards": {g: [0, 0] for g in guards.NAMES}})
        a["cost"] += r["cost"]
        a["prompts"] += r["prompts"]
        a["calls"] += r["calls"]
        a["component"] += catalog.component_cost(m, r) or 0.0
        a["sessions"] += 1
        for g, (e, o) in r["guards"].items():
            a["guards"].setdefault(g, [0, 0])
            a["guards"][g][0] += e
            a["guards"][g][1] += o
    return out


def _spread(values):
    """(median, standard deviation of the log) of the positive values; None where undefined."""
    pos = [v for v in values if v > 0]
    med = statistics.median(pos) if pos else None
    sd = statistics.stdev([math.log(v) for v in pos]) if len(pos) > 1 else None
    return med, sd


def baseline(rows, method_id="bash-output-limit", tz=None, today=None):
    m = catalog.load(method_id)
    first, last = period(today, tz)
    in_period = [r for r in rows if counted_session(r) and first <= schedule.trial_date(r["start"], tz) <= last]
    span = [r for r in in_period if spans(r, tz)]
    cost = sum(r["cost"] for r in in_period)
    pool = day_pool(rows, m, tz, today)
    days = list(pool.values())
    gt = {name: [0, 0] for name in guards.NAMES}
    for r in in_period:
        for name, (e, o) in r["guards"].items():
            gt.setdefault(name, [0, 0])
            gt[name][0] += e
            gt[name][1] += o
    cpp = [d["cost"] / d["prompts"] for d in days if d["prompts"]]
    comp = [d["component"] / d["prompts"] for d in days if d["prompts"]]
    prompts = [d["prompts"] for d in days]
    comp_cost = sum(catalog.component_cost(m, r) or 0.0 for r in in_period)
    return {
        "method": m["id"], "component": m["component"], "own_guard": m["own_guard"],
        "first_day": first.isoformat(), "last_day": last.isoformat(), "calendar_days": PERIOD_DAYS,
        "active_days": len(pool), "active_share": len(pool) / PERIOD_DAYS,
        "sessions": len(in_period), "cost": cost,
        "spanning_sessions": len(span), "spanning_session_share": len(span) / len(in_period) if in_period else None,
        "spanning_cost_share": sum(r["cost"] for r in span) / cost if cost else None,
        "days_without_prompts": sum(1 for d in days if not d["prompts"]),
        "cost_per_prompt": _spread(cpp), "prompts_per_day": _spread(prompts),
        "component_per_prompt": _spread(comp), "days_without_component": sum(1 for d in days if d["component"] <= 0),
        "component_share": comp_cost / cost if cost else None,
        "guards": {name: (e / o if o else None, e, o) for name, (e, o) in gt.items()},
    }


def _fmt(x, spec):
    return "-" if x is None else format(x, spec)


def render(b):
    lines = ["Baseline over the trial days {} to {} ({} days), method {}:".format(
        b["first_day"], b["last_day"], b["calendar_days"], b["method"])]
    lines.append("  active days {} of {} ({}); counted sessions {}; weighted cost ${:,.2f}".format(
        b["active_days"], b["calendar_days"], _fmt(b["active_share"], ".0%"), b["sessions"], b["cost"]))
    lines.append("  sessions across a 04:00 boundary: {} ({} of sessions, {} of cost)".format(
        b["spanning_sessions"], _fmt(b["spanning_session_share"], ".1%"), _fmt(b["spanning_cost_share"], ".1%")))
    med, sd = b["cost_per_prompt"]
    lines.append("  per active day: cost per typed prompt median ${}, log sd {}; days without prompts {}".format(
        _fmt(med, ".3f"), _fmt(sd, ".2f"), b["days_without_prompts"]))
    med, sd = b["prompts_per_day"]
    lines.append("  per active day: typed prompts median {}, log sd {}".format(_fmt(med, ".1f"), _fmt(sd, ".2f")))
    if b["component"]:
        med, sd = b["component_per_prompt"]
        lines.append("  component {}: {} of cost; per active day cost per prompt median ${}, log sd {} (days "
                     "without it: {})".format(b["component"], _fmt(b["component_share"], ".1%"), _fmt(med, ".4f"),
                                             _fmt(sd, ".2f"), b["days_without_component"]))
    lines.append("  guards (events / opportunities over the counted sessions):")
    for name in guards.NAMES:
        rate, e, o = b["guards"][name]
        lines.append("    {:<22} {:>8} {:>7} / {:<7}{}".format(name, _fmt(rate, ".2%"), e, o,
                                                             "  (own guard)" if name == b["own_guard"] else ""))
    return "\n".join(lines)
