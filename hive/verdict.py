"""Verdict, version 2 (plan section 5). A trial's record names this version and the SHA-256 of this file;
if the file changed between the start and the end of a trial, no record is made.

Version 1 used a percentile bootstrap over active days. On simulated null trials with 10 to 15 days per
arm it gave each one-sided false verdict about 7% of the time, and its notes rule had no error control.
Version 2 judges by re-randomization: it redraws the schedule the way the trial drew it.

Input: one aggregate per active period. A period is the sessions that started under one trial day's
applied state; it has arm, day (index in the schedule), cost, prompts, calls, guards {name: [events,
opportunities]} and notes. Days come in pairs (0, 1), (2, 3), ...; in each pair one day is on and one
off, in random order.

- Draws. A draw flips each pair with probability 1/2: the periods of a flipped pair change arm. If the
  method changes nothing, the real schedule is one of these equally likely draws. With k pairs that have
  an active period, all 2^k draws are used when 2^k <= DRAWS; otherwise the real schedule plus DRAWS
  random draws (seed from the trial file). The real schedule counts as a draw, so no p-value is 0.
- Cost: weighted cost per typed prompt, (sum of cost / sum of prompts) per arm; ratio on / off. The 90%
  interval holds the ratios r for which "every period's cost under on is r times its cost under off" is
  not rejected at ALPHA on either side. It is exact: each draw has one r at which its ratio equals the
  real schedule's, and the bounds are order statistics of those r.
- Own guard: ratio on / off of events over opportunities, interval the same way; no interval when an arm
  has no events.
- Harm: the own guard and the notes are tested together at ALPHA. A draw's p-value for each is the share
  of draws at least as high (guard rate ratio; notes per active period, on minus off); the real
  schedule's smaller p-value is ranked among the draws' smaller p-values.
- A draw in which an arm has no prompts (cost) or no opportunities (guard) has no ratio and is left out
  of that number; a flag is set when the share left out is above UNDEFINED_FLAG.
- Verdict, first match wins: harm, too_little_data (an arm under MIN_ACTIVE_DAYS active periods),
  costs_more (cost lower bound above 1.0), saves (cost upper bound below 1.0), no_saving_shown.
"""
import bisect
import hashlib
import math
import random
from pathlib import Path

VERDICT_CODE = "2"
DRAWS = 10_000
ALPHA = 0.05
MIN_ACTIVE_DAYS = 10
UNDEFINED_FLAG = 0.10
SNAP = 1e-9                 # a crossing this close to 1 is a tie at 1
VERDICTS = ("harm", "too_little_data", "costs_more", "saves", "no_saving_shown")
# columns of a pair row: the on part, then the off part
COST, PROMPTS, EVENTS, OPPS, NOTES, PERIODS = range(6)
OFF = 6


def code_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _ratio(num_on, den_on, num_off, den_off):
    if den_on <= 0 or den_off <= 0 or num_off <= 0:
        return None
    return (num_on / den_on) / (num_off / den_off)


def pairs(days, own_guard):
    """One row per pair with an active period: cost, prompts, events, opportunities, notes, periods of its
    on part, then the same of its off part."""
    rows = {}
    for d in days:
        e, o = d["guards"].get(own_guard, [0, 0])
        row = rows.setdefault(d["day"] // 2, [0.0, 0, 0, 0, 0, 0, 0.0, 0, 0, 0, 0, 0])
        base = 0 if d["arm"] == "on" else OFF
        for i, v in enumerate((d["cost"], d["prompts"], e, o, d.get("notes", 0), 1)):
            row[base + i] += v
    return [tuple(r) for _, r in sorted(rows.items())]


def patterns(k, seed, draws=DRAWS):
    """Bit patterns of the draws (bit j set: pair j flipped); the first is 0, the real schedule."""
    if 2 ** k <= draws:
        return list(range(2 ** k))
    rng = random.Random(seed)
    return [0] + [rng.getrandbits(k) for _ in range(draws)]


def _root(a2, a1, a0):
    """The x > 0 at which g(x) = a2 x^2 + a1 x + a0 (a2 >= 0, a0 <= 0) turns from negative to
    non-negative: 0.0 when g >= 0 for every x > 0, inf when g < 0 for every x > 0, None when g is 0."""
    if a2 > 0:
        if a0 < 0:
            disc = math.sqrt(a1 * a1 - 4 * a2 * a0)
            return (-2 * a0) / (a1 + disc) if a1 >= 0 else (-a1 + disc) / (2 * a2)
        return 0.0 if a1 >= 0 else -a1 / a2
    if a1 > 0:
        return -a0 / a1
    if a1 == 0 and a0 == 0:
        return None
    return math.inf


def crossing(real, stay_on, to_on, to_off, stay_off, den_on, den_off):
    """The ratio r at which a draw's ratio equals the real one, when every real on period's numerator is
    divided by r. real: (numerator on, denominator on, numerator off, denominator off) of the real
    schedule; stay_on, to_off: numerators of real on periods that the draw keeps on or moves off;
    to_on, stay_off: the same for real off periods; den_on, den_off: the draw's denominators.

    The real ratio is at least the draw's for r <= the result and at most for r >= it (the ratio of the
    two falls as r grows), so the result splits the draws between the two one-sided p-values. Returns
    None when the two are equal for every r."""
    n1, d1, n0, d0 = real
    k = n1 * d0 * den_on
    m = den_off * d1 * n0
    x = _root(k * to_off, k * stay_off - m * stay_on, -m * to_on)
    if x is None:
        return None
    r = math.inf if x == 0 else (0.0 if x == math.inf else 1.0 / x)
    return 1.0 if abs(r - 1.0) <= SNAP else r


def bounds(rs, ties, total, alpha=ALPHA):
    """(low, high) from the crossings rs of the draws, `ties` draws equal to the real schedule for every
    r, and `total` draws. r is rejected as too high when (ties + draws with crossing >= r) <= alpha *
    total, and as too low in the mirror case. None where a side is never rejected."""
    m0 = math.floor(alpha * total - ties) + 1
    if m0 <= 0 or m0 > len(rs):
        return None, None
    asc = sorted(rs)
    low, high = asc[m0 - 1], asc[len(asc) - m0]
    return (None if math.isinf(low) else low), (None if math.isinf(high) else high)


def _rate_ratio(e1, o1, e0, o0):
    """On / off rate as one correctly rounded division of integers; -inf when it shows nothing."""
    if o1 <= 0 or o0 <= 0:
        return -math.inf
    if e0 == 0:
        return math.inf if e1 > 0 else -math.inf
    return (e1 * o0) / (o1 * e0)


def _notes_gap(n1, u1, n0, u0):
    """Notes per active period, on minus off, as one division of integers; -inf without periods."""
    if u1 <= 0 or u0 <= 0:
        return -math.inf
    return (n1 * u0 - n0 * u1) / (u1 * u0)


def randomize(rows, seed, draws=DRAWS):
    """Cost and guard intervals, the joint harm p-value and the shares of draws without a ratio."""
    pats = patterns(len(rows), seed, draws)
    tot = [sum(col) for col in zip(*rows)] if rows else [0.0, 0, 0, 0, 0, 0, 0.0, 0, 0, 0, 0, 0]
    c_real = (tot[COST], tot[PROMPTS], tot[OFF + COST], tot[OFF + PROMPTS])
    g_real = (tot[EVENTS], tot[OPPS], tot[OFF + EVENTS], tot[OFF + OPPS])
    g_interval = all(v > 0 for v in g_real)
    c_rs, c_ties, c_n = [], 0, 0
    g_rs, g_ties, g_n = [], 0, 0
    g_stat, n_stat = [], []
    width = len(tot)
    for pat in pats:
        f = [0.0, 0, 0, 0, 0, 0, 0.0, 0, 0, 0, 0, 0]
        j, bits = 0, pat
        while bits:
            if bits & 1:
                row = rows[j]
                for i in range(width):
                    f[i] += row[i]
            bits >>= 1
            j += 1

        def on(col):
            return tot[col] - f[col] + f[OFF + col]

        def off(col):
            return tot[OFF + col] - f[OFF + col] + f[col]

        p1, p0 = on(PROMPTS), off(PROMPTS)
        if p1 > 0 and p0 > 0 and c_real[2] > 0:
            c_n += 1
            r = None if pat == 0 else crossing(c_real, tot[COST] - f[COST], f[OFF + COST], f[COST],
                                               tot[OFF + COST] - f[OFF + COST], p1, p0)
            if r is None:
                c_ties += 1
            else:
                c_rs.append(r)
        o1, o0 = on(OPPS), off(OPPS)
        if o1 > 0 and o0 > 0:
            g_n += 1
            if g_interval:
                r = None if pat == 0 else crossing(g_real, tot[EVENTS] - f[EVENTS], f[OFF + EVENTS], f[EVENTS],
                                                   tot[OFF + EVENTS] - f[OFF + EVENTS], o1, o0)
                if r is None:
                    g_ties += 1
                else:
                    g_rs.append(r)
        g_stat.append(_rate_ratio(on(EVENTS), o1, off(EVENTS), o0))
        n_stat.append(_notes_gap(on(NOTES), on(PERIODS), off(NOTES), off(PERIODS)))
    total = len(pats)
    sg, sn = sorted(g_stat), sorted(n_stat)
    smaller = [min(total - bisect.bisect_left(sg, g), total - bisect.bisect_left(sn, n))
               for g, n in zip(g_stat, n_stat)]
    harm_p = sum(1 for s in smaller if s <= smaller[0]) / total
    c_low, c_high = bounds(c_rs, c_ties, c_n)
    g_low, g_high = bounds(g_rs, g_ties, g_n) if g_interval else (None, None)
    return {"draws": total, "exact": 2 ** len(rows) <= draws,
            "cost": (c_low, c_high), "guard": (g_low, g_high), "harm_p": harm_p,
            "undefined": {"cost": (total - c_n) / total, "guard": (total - g_n) / total}}


def judge(days, own_guard, seed, draws=DRAWS):
    """days: active periods as dicts with arm ("on"/"off"), day, cost, prompts, calls, guards
    {name: [events, opportunities]} and notes. own_guard: guard name. Returns the verdict dict."""
    arms = {"on": [d for d in days if d["arm"] == "on"], "off": [d for d in days if d["arm"] == "off"]}
    out = {"arms": {}, "flags": []}
    for arm, ds in arms.items():
        cost = sum(d["cost"] for d in ds)
        prompts = sum(d["prompts"] for d in ds)
        calls = sum(d["calls"] for d in ds)
        g = {}
        for d in ds:
            for name, (e, o) in d["guards"].items():
                row = g.setdefault(name, [0, 0])
                row[0] += e
                row[1] += o
        out["arms"][arm] = {"active_days": len(ds), "prompts": prompts, "cost": cost, "calls": calls,
                            "cost_per_prompt": cost / prompts if prompts else None,
                            "calls_per_prompt": calls / prompts if prompts else None,
                            "guards": {k: (e / o if o else None) for k, (e, o) in sorted(g.items())},
                            "guard_counts": g, "notes": sum(d.get("notes", 0) for d in ds)}
    a1, a0 = out["arms"]["on"], out["arms"]["off"]
    (e1, o1), (e0, o0) = a1["guard_counts"].get(own_guard, [0, 0]), a0["guard_counts"].get(own_guard, [0, 0])
    rz = randomize(pairs(days, own_guard), seed, draws)
    out["cost_ratio"] = {"estimate": _ratio(a1["cost"], a1["prompts"], a0["cost"], a0["prompts"]),
                         "low": rz["cost"][0], "high": rz["cost"][1]}
    out["own_guard_ratio"] = {"estimate": _ratio(e1, o1, e0, o0),
                              "low": rz["guard"][0], "high": rz["guard"][1]}
    out["harm_p"] = rz["harm_p"]
    out["draws"] = {"count": rz["draws"], "exact": rz["exact"]}
    out["undefined_draws"] = rz["undefined"]
    if rz["undefined"]["cost"] > UNDEFINED_FLAG:
        out["flags"].append("cost_undefined_draws")
    if rz["undefined"]["guard"] > UNDEFINED_FLAG:
        out["flags"].append("guard_undefined_draws")
    out["notes"] = {"on": a1["notes"], "off": a0["notes"]}
    c = out["cost_ratio"]
    if rz["harm_p"] <= ALPHA:
        v = "harm"
    elif a1["active_days"] < MIN_ACTIVE_DAYS or a0["active_days"] < MIN_ACTIVE_DAYS:
        v = "too_little_data"
    elif c["low"] is not None and c["low"] > 1.0:
        v = "costs_more"
    elif c["high"] is not None and c["high"] < 1.0:
        v = "saves"
    else:
        v = "no_saving_shown"
    out["verdict"] = v
    out["verdict_code"] = VERDICT_CODE
    return out
