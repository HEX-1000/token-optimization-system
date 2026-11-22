"""Simulated trials of the verdict: the false verdict rates of null trials and a power table.

    python -m hive simulate null  [--trials 1000] [--days 10] [--sigma 0.5] [--active 1.0]
    python -m hive simulate power [--trials 1000] [--sigma 0.5] [--active 1.0]
    python -m hive simulate power --own [--component] [--ratio R ...] [--trials 1000] [--active A]

A trial follows the schedule of trial.tick: days in pairs, one on and one off in random order; each day
is active with probability `active`; at the start of each pair the trial stops when both arms have
`days` active days, and it stops after the catalog entry's max_calendar_days in any case (`max_days`;
MAX_DAYS, the value of DEFAULT_METHOD, when none is given; the command passes the value of --method).
With active 1.0 a trial has exactly `days` pairs.

Model of one active day in one arm (the same in both arms of a null trial):
- typed prompts: lognormal, median PROMPTS_MEDIAN, sigma PROMPTS_SIGMA, rounded, at least 1;
- weighted cost per prompt: lognormal, median 1 (times the true ratio in the on arm), sigma `sigma`
  (plan step 3: 0.5); the day's cost is prompts times that;
- own-guard opportunities: lognormal, median GUARD_OPP_MEDIAN, sigma 0.7, at least 1; events binomial
  with probability GUARD_P;
- notes: Poisson with mean `note_rate`.
Each trial runs verdict.judge with its full number of draws and a seed drawn from the run's seed.

With --own the days are not drawn from the model above but from the node's own active days of the last
28 trial days (hive.baseline.day_pool): each simulated active day is one of them, drawn with replacement,
with its cost, prompts and own-guard counts; in the on arm its cost (or with --component, the component's
cost, which then stands in the place of the cost) is multiplied by the true ratio. Days are active with
the node's share of active days unless --active is given. The total-cost table counts verdict `saves`;
the component table counts trials whose component interval lies below 1.0 (its upper bound under 1.0),
which is what a record reports and the verdict does not judge. Rows: the ratios of RATIOS, the extra
ratios given, and 1.0 as the null check.
"""
import math
import random
from collections import Counter
from multiprocessing import Pool

from . import catalog, verdict

PROMPTS_MEDIAN, PROMPTS_SIGMA = 15, 0.7
GUARD_OPP_MEDIAN, GUARD_OPP_SIGMA, GUARD_P = 20, 0.7, 0.05
RATIOS = (0.6, 0.7, 0.8, 0.9)
DAYS = (10, 15)
NOTE_RATES = (0.0, 0.1, 0.3)
DEFAULT_METHOD = "bash-output-limit"


def max_days(method_id=DEFAULT_METHOD):
    """A trial's maximum length in calendar days: the catalog entry's max_calendar_days."""
    return catalog.load(method_id)["max_calendar_days"]


MAX_DAYS = max_days()


def poisson(rng, lam):
    if lam <= 0:
        return 0
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def day(rng, arm, idx, ratio, sigma, note_rate):
    prompts = max(1, round(rng.lognormvariate(math.log(PROMPTS_MEDIAN), PROMPTS_SIGMA)))
    cpp = rng.lognormvariate(0.0, sigma) * (ratio if arm == "on" else 1.0)
    opp = max(1, round(rng.lognormvariate(math.log(GUARD_OPP_MEDIAN), GUARD_OPP_SIGMA)))
    events = sum(1 for _ in range(opp) if rng.random() < GUARD_P)
    return {"arm": arm, "day": idx, "cost": prompts * cpp, "prompts": prompts, "calls": prompts * 7,
            "guards": {"own": [events, opp]}, "notes": poisson(rng, note_rate)}


def trial_days(rng, days, ratio, sigma, note_rate, active=1.0, max_days=None):
    """The active periods of one simulated trial."""
    limit = MAX_DAYS if max_days is None else max_days
    out, count = [], Counter()
    for idx in range(0, limit, 2):
        if count["on"] >= days and count["off"] >= days:
            break
        pair = ["on", "off"] if rng.random() < 0.5 else ["off", "on"]
        for i, arm in enumerate(pair):
            if idx + i < limit and (active >= 1.0 or rng.random() < active):
                out.append(day(rng, arm, idx + i, ratio, sigma, note_rate))
                count[arm] += 1
    return out


def trial_days_pool(rng, days, ratio, pool, active, field, own_guard, max_days=None):
    """The active periods of one simulated trial whose days are drawn from a pool of real days."""
    limit = MAX_DAYS if max_days is None else max_days
    out, count = [], Counter()
    for idx in range(0, limit, 2):
        if count["on"] >= days and count["off"] >= days:
            break
        pair = ["on", "off"] if rng.random() < 0.5 else ["off", "on"]
        for i, arm in enumerate(pair):
            if idx + i < limit and (active >= 1.0 or rng.random() < active):
                d = pool[rng.randrange(len(pool))]
                out.append({"arm": arm, "day": idx + i, "cost": d[field] * (ratio if arm == "on" else 1.0),
                            "prompts": d["prompts"], "calls": d["calls"],
                            "guards": {own_guard: list(d["guards"].get(own_guard, [0, 0]))}, "notes": 0})
                count[arm] += 1
    return out


def one_trial_pool(args):
    seed, days, ratio, pool, active, field, own_guard, draws, limit = args
    rng = random.Random(seed)
    ds = trial_days_pool(rng, days, ratio, pool, active, field, own_guard, limit)
    v = verdict.judge(ds, own_guard, rng.getrandbits(64), draws)
    high = v["cost_ratio"]["high"]
    return v["verdict"], high is not None and high < 1.0


def run_pool(trials, days, ratio, pool, active, field, own_guard, seed, draws=verdict.DRAWS, processes=None,
             max_days=None):
    """Counter of verdicts and the number of trials whose cost interval lies below 1.0."""
    rng = random.Random(seed)
    jobs = [(rng.getrandbits(64), days, ratio, pool, active, field, own_guard, draws, max_days)
            for _ in range(trials)]
    with Pool(processes) as pool_:
        res = pool_.map(one_trial_pool, jobs, chunksize=max(1, trials // 64))
    return Counter(v for v, _ in res), sum(1 for _, below in res if below)


def power_table_own(pool, active, own_guard, trials=1000, component=False, extra_ratios=(), seed=20261121,
                    max_days=None):
    """Power with the node's own days. pool: list of day aggregates (hive.baseline.day_pool values)."""
    limit = MAX_DAYS if max_days is None else max_days
    field = "component" if component else "cost"
    ratios = sorted(set(RATIOS) | {round(r, 4) for r in extra_ratios} | {1.0})
    what = ("share of trials whose component interval lies below 1.0" if component
            else "share of trials with verdict `saves`")
    lines = ["Power with the node's own days, verdict v{}: {}; {} real active days in the pool, days active with "
             "probability {:.2f}, own guard {}, {} trials per cell, at most {} calendar days".format(
                 verdict.VERDICT_CODE, what, len(pool), active, own_guard, trials, limit),
             "| true {} ratio | ".format("component" if component else "cost") +
             " | ".join("{} active days per arm".format(d) for d in DAYS) + " | too_little_data at {} |".format(DAYS[0]),
             "|---|" + "---|" * (len(DAYS) + 1)]
    for i, ratio in enumerate(ratios):
        cells, tld = [], None
        for j, d in enumerate(DAYS):
            c, below = run_pool(trials, d, ratio, pool, active, field, own_guard, seed + 10 * i + j,
                                max_days=limit)
            cells.append("{:.1%}".format((below if component else c["saves"]) / trials))
            if j == 0:
                tld = c["too_little_data"] / trials
        lines.append("| {} | {} | {:.1%} |".format(ratio, " | ".join(cells), tld))
    return "\n".join(lines)


def one_trial(args):
    seed, days, ratio, sigma, note_rate, active, draws, limit = args
    rng = random.Random(seed)
    ds = trial_days(rng, days, ratio, sigma, note_rate, active, limit)
    return verdict.judge(ds, "own", rng.getrandbits(64), draws)["verdict"]


def run(trials, days, ratio, sigma, note_rate, seed, active=1.0, draws=verdict.DRAWS, processes=None,
        max_days=None):
    rng = random.Random(seed)
    jobs = [(rng.getrandbits(64), days, ratio, sigma, note_rate, active, draws, max_days) for _ in range(trials)]
    with Pool(processes) as pool:
        verdicts = pool.map(one_trial, jobs, chunksize=max(1, trials // 64))
    return Counter(verdicts)


def null_table(trials=1000, days=10, sigma=0.5, active=1.0, note_rates=NOTE_RATES, seed=20261112, max_days=None):
    limit = MAX_DAYS if max_days is None else max_days
    lines = ["Null trials (true ratio 1.0), verdict v{}: share of each verdict; {} trials per row, {} active days "
             "per arm, days active with probability {}, cost per prompt sigma {}, at most {} calendar days".format(
                 verdict.VERDICT_CODE, trials, days, active, sigma, limit),
             "| notes per active day | saves | costs_more | harm | no_saving_shown | too_little_data |",
             "|---|---|---|---|---|---|"]
    for i, nr in enumerate(note_rates):
        c = run(trials, days, 1.0, sigma, nr, seed + i, active, max_days=limit)
        lines.append("| {} | {} |".format(nr, " | ".join(
            "{:.1%}".format(c[v] / trials) for v in ("saves", "costs_more", "harm", "no_saving_shown",
                                                     "too_little_data"))))
    return "\n".join(lines)


def power_table(trials=1000, sigma=0.5, active=1.0, note_rate=0.0, seed=20261113, max_days=None):
    limit = MAX_DAYS if max_days is None else max_days
    lines = ["Power, verdict v{}: share of trials with verdict `saves`; {} trials per cell, days active with "
             "probability {}, cost per prompt sigma {}, notes {} per active day, at most {} calendar days".format(
                 verdict.VERDICT_CODE, trials, active, sigma, note_rate, limit),
             "| true ratio | " + " | ".join("{} active days per arm".format(d) for d in DAYS) + " |",
             "|---|" + "---|" * len(DAYS)]
    for i, ratio in enumerate(RATIOS):
        cells = []
        for j, d in enumerate(DAYS):
            c = run(trials, d, ratio, sigma, note_rate, seed + 10 * i + j, active, max_days=limit)
            cells.append("{:.1%}".format(c["saves"] / trials))
        lines.append("| {} | {} |".format(ratio, " | ".join(cells)))
    return "\n".join(lines)
