"""The trial loop: `hive trial start|status|stop`, `hive tick`, `hive keep` (plan section 5).

Files in the data folder: trial.json (the running trial, written before anything changes),
arm_log.jsonl (every state applied to the settings file, with its time), trials/<id>.json (finished
trials with their verdict), notes.jsonl (`hive note`), kept.json (methods kept on), backups/,
tick_errors.jsonl (ticks that failed).

Safety: a trial changes the settings file named by HIVE_CLAUDE_SETTINGS or the config. It changes the
real user settings file (~/.claude/settings.json) only as a live trial: one started with HIVE_ALLOW_LIVE=1
set, by the user at the keyboard. The start writes `live` into the trial file, so that the scheduled tick
and the stop, which run without that variable, may change that file for this trial only; and only a live
trial creates the scheduled task. Any other trial is a dry run of the task (hive.schedule).
"""
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import catalog, schedule, settings, store, verdict
from . import weights as _weights

ALLOW_LIVE = "HIVE_ALLOW_LIVE"
SETTINGS_ENV = "HIVE_CLAUDE_SETTINGS"
EXCLUDED_FLAG = 0.10
STOP_REASONS = ("schedule", "max_days", "harm", "other")


class TrialError(RuntimeError):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def real_settings():
    return Path.home() / ".claude" / "settings.json"


def config():
    return store.read_json(store.data_dir() / "config.json", {}) or {}


def settings_file():
    p = os.environ.get(SETTINGS_ENV) or config().get("settings_path")
    return Path(p) if p else real_settings()


def is_real(path):
    return os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(os.path.abspath(str(real_settings())))


def check_live(path, t=None):
    """Refuse the real user settings file unless HIVE_ALLOW_LIVE=1 is set or `t` is a live trial whose
    settings file it is."""
    if not is_real(path) or os.environ.get(ALLOW_LIVE) == "1":
        return
    if t is not None and t.get("live") is True and is_real(t["settings_file"]):
        return
    raise TrialError("the real user settings file is changed only by a live trial; start it with {}=1 at the "
                     "keyboard, or set {} to a test file".format(ALLOW_LIVE, SETTINGS_ENV))


def paths():
    d = store.data_dir()
    return {"trial": d / "trial.json", "arm_log": d / "arm_log.jsonl", "trials": d / "trials",
            "notes": d / "notes.jsonl", "kept": d / "kept.json", "backups": d / "backups",
            "sessions": d / "sessions.jsonl", "tick_errors": d / "tick_errors.jsonl"}


def current():
    return store.read_json(paths()["trial"])


def log(event, now, **fields):
    row = {"time": iso(now), "event": event}
    row.update(fields)
    store.append_jsonl(paths()["arm_log"], row)


def arm_log(trial_id):
    return [r for r in store.read_jsonl(paths()["arm_log"]) if r.get("trial") == trial_id]


# ------------------------------------------------------------------ start

def start(method_id, value, now=None, rand=os.urandom, tz=None, weights_id=_weights.DEFAULT,
          eligibility=None, replay_result=None):
    """Write the trial file, then create the scheduled task (a live trial) or write its dry run. Nothing in
    the settings changes before the first tick of the first trial day. If the task cannot be created, the
    trial file is removed again and nothing has changed.

    eligibility: (metric value, minimum) computed by the caller; replay_result: the replay's verdict for
    this value or None for a method without replay."""
    p = paths()
    if current() is not None:
        raise TrialError("a trial is already running")
    m = catalog.load(method_id)
    name, allowed = catalog.param(m)
    if value not in allowed:
        raise TrialError("{} must be one of {}".format(name, allowed))
    _weights.load(weights_id)
    if eligibility is None or eligibility[0] is None or eligibility[0] < eligibility[1]:
        raise TrialError("the method is not eligible on this node")
    if m["replay"] and not (replay_result and replay_result.get("passes")):
        raise TrialError("the replay screen did not pass for this value")
    path = settings_file()
    check_live(path)
    live = is_real(path)
    text, existed = settings.read(path)
    now = now or now_utc()
    first_day = schedule.trial_date(now, tz) + timedelta(days=1)
    t = {"trial_id": rand(8).hex(), "method": {"id": m["id"], "version": m["version"], "params": {name: value}},
         "own_guard": m["own_guard"], "setting_path": m["setting"]["path"],
         "on_text": catalog.on_value_text(m, value), "settings_file": str(path), "live": live, "file_existed": existed,
         "prior": settings.get_raw(text, m["setting"]["path"]), "parent_created": False,
         "weights": weights_id, "verdict_code": verdict.VERDICT_CODE, "verdict_hash": verdict.code_hash(),
         "seed": int.from_bytes(rand(8), "big"), "start_date": first_day.isoformat(),
         "schedule": schedule.draw(m["max_calendar_days"], rand),
         "min_active_days": m["min_active_days_per_arm"], "max_days": m["max_calendar_days"],
         "eligibility": {"value": eligibility[0], "min": eligibility[1]},
         "replay": replay_result, "created": iso(now), "status": "running"}
    store.write_json(p["trial"], t)
    log("start", now, trial=t["trial_id"])
    try:
        plan = schedule.create_task(store.data_dir(), dry_run=not live)
    except schedule.TaskError as exc:
        p["trial"].unlink()
        log("start_failed", now, trial=t["trial_id"])
        raise TrialError("{}; the trial file was removed and nothing changed".format(exc)) from None
    return t, plan


# ------------------------------------------------------------------ tick

def _write_settings(t, target, now):
    """Set the method's key to `target` (JSON text, or None to remove it). Returns (ok, changed outside)."""
    path = Path(t["settings_file"])
    check_live(path, t)
    text, existed = settings.read(path)
    cur = settings.get_raw(text, t["setting_path"])
    applied = [r for r in arm_log(t["trial_id"]) if r["event"] == "applied"]
    outside = bool(applied) and cur != applied[-1]["value_text"]
    if existed:
        b = paths()["backups"]
        b.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(path), str(b / "settings-{}.json".format(now.strftime("%Y%m%dT%H%M%S%f"))))
    if target is None:
        new = settings.remove(text, t["setting_path"], drop_empty_parent=t["parent_created"])
    else:
        new, created = settings.set_raw(text, t["setting_path"], target)
        if created:
            t["parent_created"] = True
    if new != text or not existed:
        settings.write(path, new)
    back, _ = settings.read(path)
    return settings.get_raw(back, t["setting_path"]) == target, outside


def overrides(t, rows, environ=None):
    """Where else the same behavior is set: 'user', 'project', 'environment' (plan section 5, `overridden`)."""
    environ = os.environ if environ is None else environ
    m = catalog.load(t["method"]["id"])
    o = m["overridden_by"]
    found = set()

    def scan(text, skip_own):
        hit = False
        for p in o["settings_paths"]:
            if skip_own and p == m["setting"]["path"]:
                continue
            try:
                if settings.get_raw(text, p) is not None:
                    hit = True
            except settings.SettingsError:
                pass
        key = o.get("model_settings_key")
        if key:
            try:
                ms = json.loads(text).get("modelSettings")
            except ValueError:
                ms = None
            if isinstance(ms, dict) and any(isinstance(v, dict) and key in v for v in ms.values()):
                hit = True
        return hit

    text, _ = settings.read(t["settings_file"])
    if scan(text, True):
        found.add("user")
    for proj in sorted({r.get("project") for r in rows if r.get("project")}):
        for name in ("settings.json", "settings.local.json"):
            f = Path(proj) / ".claude" / name
            if f.is_file():
                try:
                    if scan(f.read_text(encoding="utf-8"), False):
                        found.add("project")
                except OSError:
                    pass
    if any(e in environ for e in o["environment"]):
        found.add("environment")
    return sorted(found)


def active_days(t, rows, tz=None, until=None):
    """{'on': n, 'off': n} from the sessions that count (no cost or guard numbers)."""
    days, _excl = collect(t, rows, [], tz, until)
    out = {"on": 0, "off": 0}
    for d in days:
        out[d["arm"]] += 1
    return out


def tick(now=None, tz=None, dirs=None, run_sensor=True, environ=None):
    """Run the sensor, apply the trial day's state and log it, stop when the trial is over. A sensor error
    is logged (`sense_error`) and does not keep the day's state from being applied."""
    from . import sense
    now = now or now_utc()
    sense_error = None
    if run_sensor:
        try:
            sense.sense(dirs)
        except Exception as exc:
            sense_error = type(exc).__name__
    t = current()
    if t is None:
        return {"trial": None}
    if sense_error:
        log("sense_error", now, trial=t["trial_id"], error=sense_error)
    idx = (schedule.trial_date(now, tz) - datetime.fromisoformat(t["start_date"]).date()).days
    if idx < 0:
        return {"trial": t["trial_id"], "waiting": True}
    rows = store.read_jsonl(paths()["sessions"])
    if idx >= t["max_days"]:
        return {"trial": t["trial_id"], "stopped": stop("max_days", now, tz, dirs, run_sensor=False)}
    if idx % 2 == 0 and idx > 0:
        ad = active_days(t, rows, tz, until=now)
        if ad["on"] >= t["min_active_days"] and ad["off"] >= t["min_active_days"]:
            return {"trial": t["trial_id"], "stopped": stop("schedule", now, tz, dirs, run_sensor=False)}
    state = t["schedule"][idx]
    target = t["on_text"] if state == "on" else t["prior"]
    ok, outside = _write_settings(t, target, now)
    store.write_json(paths()["trial"], t)
    if outside:
        log("settings_changed", now, trial=t["trial_id"])
    log("applied", now, trial=t["trial_id"], day=idx, state=state, value_text=target, ok=ok)
    for where in overrides(t, rows, environ):
        log("overridden", now, trial=t["trial_id"], where=where)
    return {"trial": t["trial_id"], "day": idx, "state": state, "ok": ok}


# ------------------------------------------------------------------ verdict input

def counted_session(r):
    """A sessions.jsonl row that a trial counts: interactive (entrypoint cli), a main file with at least one
    counted call, and a start and an end."""
    return r.get("entrypoint") == "cli" and bool(r.get("has_main")) and r.get("main_calls", 0) >= 1         and bool(r.get("start")) and bool(r.get("end"))


def _period_at(applied, when):
    """(day, state) of the last applied entry at or before `when`, or None."""
    per = None
    for time_, day, state in applied:
        if time_ <= when:
            per = (day, state)
        else:
            break
    return per


def collect(t, rows, notes, tz=None, until=None):
    """(active periods, (excluded sessions, sessions in the period)) for verdict.judge.

    A period is one trial day's applied state: the sessions that started after it was applied and before
    the next day's state was. A session counts only when no other day's state was applied, and the trial
    was not restored, before its last record: Claude Code reloads settings during a session, so such a
    session saw two states, and its period would depend on the other day's draw. A note counts in the
    period in effect at its time when that period is active.

    Each period also has `component` (weighted cost of the method's component, 0.0 when it has none) and
    `not_applied` (its on-state sessions whose applied check shows the setting did not apply)."""
    m = catalog.load(t["method"]["id"])
    value = next(iter(t["method"]["params"].values()))
    entries = arm_log(t["trial_id"])
    applied = sorted((parse(r["time"]), r["day"], r["state"]) for r in entries if r["event"] == "applied")
    if not applied:
        return [], (0, 0)
    first = applied[0][0]
    restored = next((parse(r["time"]) for r in entries if r["event"] == "restored"), None)
    stop_t = restored or until
    start_date = datetime.fromisoformat(t["start_date"]).date()
    agg, excluded, seen = {}, 0, 0
    for r in rows:
        if not counted_session(r):
            continue
        s, e = parse(r["start"]), parse(r["end"])
        if s < first or (stop_t is not None and s >= stop_t):
            continue
        idx = (schedule.trial_date(s, tz) - start_date).days
        if idx < 0 or idx >= len(t["schedule"]):
            continue
        seen += 1
        day, state = _period_at(applied, s)
        spans = any(s < time_ <= e and d != day for time_, d, _ in applied) or \
            (restored is not None and s < restored <= e)
        if spans:
            excluded += 1
            continue
        d = agg.setdefault(day, {"arm": state, "day": day, "cost": 0.0, "prompts": 0, "calls": 0,
                                 "sessions": 0, "notes": 0, "guards": {}, "component": 0.0, "not_applied": 0})
        d["component"] += catalog.component_cost(m, r) or 0.0
        if state == "on" and catalog.not_applied(m, value, r):
            d["not_applied"] += 1
        d["cost"] += r["cost"]
        d["prompts"] += r["prompts"]
        d["calls"] += r["calls"]
        d["sessions"] += 1
        for name, (ev, op) in r["guards"].items():
            g = d["guards"].setdefault(name, [0, 0])
            g[0] += ev
            g[1] += op
    for note in notes:
        when = parse(note["time"])
        if when < first or (stop_t is not None and when >= stop_t):
            continue
        day, _state = _period_at(applied, when)
        if day in agg:
            agg[day]["notes"] += 1
    return [agg[k] for k in sorted(agg)], (excluded, seen)


# ------------------------------------------------------------------ stop

def stop(reason, now=None, tz=None, dirs=None, run_sensor=True):
    """Restore the key, remove the task, run the sensor, compute the verdict, archive the trial."""
    from . import sense
    if reason not in STOP_REASONS:
        raise TrialError("reason must be one of {}".format(STOP_REASONS))
    now = now or now_utc()
    t = current()
    if t is None:
        raise TrialError("no trial is running")
    ok, outside = _write_settings(t, t["prior"], now)
    path = Path(t["settings_file"])
    if not t["file_existed"] and settings.read(path)[0] == "{}\n":
        path.unlink()
    if outside:
        log("settings_changed", now, trial=t["trial_id"])
    log("restored", now, trial=t["trial_id"], state="off", value_text=t["prior"], ok=ok)
    removed = schedule.remove_task(store.data_dir(), dry_run=not t.get("live"))["removed"]
    if removed is not None:
        log("task_removed", now, trial=t["trial_id"], ok=removed)
    if run_sensor:
        sense.sense(dirs)
    t["status"] = "finished"
    t["stop"] = {"time": iso(now), "reason": reason}
    t["result"] = compute(t, tz)
    p = paths()
    p["trials"].mkdir(parents=True, exist_ok=True)
    store.write_json(p["trials"] / (t["trial_id"] + ".json"), t)
    p["trial"].unlink()
    return t["result"]["verdict"]


def compute(t, tz=None):
    if verdict.code_hash() != t["verdict_hash"]:
        return {"verdict": None, "error": "verdict_code_changed"}
    rows = store.read_jsonl(paths()["sessions"])
    notes = store.read_jsonl(paths()["notes"])
    days, (excluded, seen) = collect(t, rows, notes, tz)
    out = verdict.judge(days, t["own_guard"], t["seed"])
    out["component_ratio"] = component_ratio(t, days)
    out["cost_per_call_ratio"] = cost_per_call_ratio(t, days)
    events = {r["event"] for r in arm_log(t["trial_id"])}
    for ev in ("settings_changed", "overridden"):
        if ev in events:
            out["flags"].append(ev)
    reason = t.get("stop", {}).get("reason")
    if reason in ("harm", "other"):
        out["flags"].append("stopped_by_user_" + reason)
    if reason == "max_days":
        out["flags"].append("max_calendar_days")
    if seen and excluded / seen > EXCLUDED_FLAG:
        out["flags"].append("excluded_sessions_over_10pct")
    n_not_applied = sum(d["not_applied"] for d in days)
    if n_not_applied:
        out["flags"].append("not_applied")
    out["sessions"] = {"counted": seen - excluded, "excluded": excluded, "not_applied": n_not_applied}
    return out


def component_ratio(t, days):
    """Ratio on / off of the component's weighted cost per typed prompt, with its 90% interval by the same
    re-randomization and seed as the cost interval (hive.verdict). Reported in the record, not judged.
    None when the method has no component."""
    m = catalog.load(t["method"]["id"])
    if m["component"] is None:
        return None
    as_cost = [dict(d, cost=d["component"]) for d in days]
    rz = verdict.randomize(verdict.pairs(as_cost, t["own_guard"]), t["seed"])
    arm = {a: [sum(d["component"] for d in days if d["arm"] == a), sum(d["prompts"] for d in days if d["arm"] == a)]
           for a in ("on", "off")}
    (c1, p1), (c0, p0) = arm["on"], arm["off"]
    estimate = (c1 / p1) / (c0 / p0) if p1 > 0 and p0 > 0 and c0 > 0 else None
    return {"estimate": estimate, "low": rz["cost"][0], "high": rz["cost"][1]}


def cost_per_call_ratio(t, days):
    """Ratio on / off of the weighted cost per counted call, with its 90% interval: the cost interval of
    hive.verdict with counted calls in the place of typed prompts, by the same re-randomization and seed.
    Reported in the record, not judged: cost per call does not see the calls a method adds
    (eval/hive/STEP5_DECISIONS.md section 4)."""
    per_call = [dict(d, prompts=d["calls"]) for d in days]
    rz = verdict.randomize(verdict.pairs(per_call, t["own_guard"]), t["seed"])
    arm = {a: [sum(d["cost"] for d in days if d["arm"] == a), sum(d["calls"] for d in days if d["arm"] == a)]
           for a in ("on", "off")}
    (c1, n1), (c0, n0) = arm["on"], arm["off"]
    estimate = (c1 / n1) / (c0 / n0) if n1 > 0 and n0 > 0 and c0 > 0 else None
    return {"estimate": estimate, "low": rz["cost"][0], "high": rz["cost"][1]}


def status(tz=None, now=None):
    """Days done and active days per arm; never cost or guard numbers."""
    t = current()
    if t is None:
        return None
    now = now or now_utc()
    idx = (schedule.trial_date(now, tz) - datetime.fromisoformat(t["start_date"]).date()).days
    rows = store.read_jsonl(paths()["sessions"])
    applied = [r for r in arm_log(t["trial_id"]) if r["event"] == "applied"]
    errors = [r for r in store.read_jsonl(paths()["tick_errors"]) if r.get("time", "") >= t["created"]]
    return {"method": t["method"], "start_date": t["start_date"], "days_done": max(0, idx),
            "max_days": t["max_days"], "min_active_days": t["min_active_days"],
            "active_days": active_days(t, rows, tz, until=now),
            "last_tick": applied[-1]["time"] if applied else None,
            "last_tick_ok": applied[-1]["ok"] if applied else None,
            "tick_errors": len(errors)}


def tick_error(exc, now=None):
    """Append a failed tick to tick_errors.jsonl: the scheduled task has no console."""
    store.append_jsonl(paths()["tick_errors"], {"time": iso(now or now_utc()),
                                                "error": "{}: {}".format(type(exc).__name__, exc)})


# ------------------------------------------------------------------ keep

def finished(method_id=None):
    d = paths()["trials"]
    out = [store.read_json(f) for f in sorted(d.glob("*.json"))] if d.is_dir() else []
    out = [x for x in out if method_id is None or x["method"]["id"] == method_id]
    return sorted(out, key=lambda x: x["stop"]["time"])


def keep(method_id, now=None):
    """Keep a method on after a `saves` verdict: set its key to the trial's on value and list it."""
    done = finished(method_id)
    if not done or done[-1]["result"].get("verdict") != "saves":
        raise TrialError("the last finished trial of {} does not have the verdict saves".format(method_id))
    t = done[-1]
    path = Path(t["settings_file"])
    check_live(path)
    text, _ = settings.read(path)
    new, _ = settings.set_raw(text, t["setting_path"], t["on_text"])
    settings.write(path, new)
    kept = store.read_json(paths()["kept"], []) or []
    kept = [k for k in kept if k["id"] != method_id] + [dict(t["method"])]
    store.write_json(paths()["kept"], kept)
    return kept
