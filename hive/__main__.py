"""Command line: python -m hive <command>. Run `python -m hive -h` for the list."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, baseline, catalog, record, replay, schedule, sense, store, suggest, trial


def _dirs(args):
    return [Path(d) for d in args.dir] if args.dir else sense.default_dirs()


def cmd_report(args):
    from . import report
    if args.days < 1:
        sys.exit("--days must be 1 or more")
    text, code = report.run(args.dir, args.days, args.names)
    print(text)
    return code


def cmd_sense(args):
    r = sense.sense(_dirs(args))
    print("sessions on disk {sessions_on_disk}; rows built {rows_built}, unchanged {rows_unchanged}, "
          "kept from deleted logs {rows_of_deleted_logs}; rows in the store {rows}".format(**r))


def cmd_note(args):
    store.append_jsonl(store.data_dir() / "notes.jsonl",
                       {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "text": args.text})
    print("Note saved locally. Only the count of notes is ever used.")


def cmd_config(args):
    path = store.data_dir() / "config.json"
    cfg = store.read_json(path, {}) or {}
    if args.plan:
        cfg["plan"] = args.plan
    if args.settings:
        cfg["settings_path"] = str(Path(args.settings).resolve())
    store.write_json(path, cfg)
    print(json.dumps(cfg, indent=2, sort_keys=True))


def cmd_trial(args):
    if args.action == "start":
        m = catalog.load(args.method)
        name, values = catalog.param(m)
        value = int(args.value)
        rows = store.read_jsonl(store.data_dir() / "sessions.jsonl")
        met = suggest.metrics(rows, datetime.now(timezone.utc).date())
        elig = suggest.eligibility(m, met, value)
        rp = None
        if m["replay"] == "bash_output_limit":
            rp = replay.summary(replay.run(_dirs(args), values), value) if value in values else None
        path = trial.settings_file()
        try:
            trial.check_live(path)
        except trial.TrialError as exc:
            print("Not started: {}".format(exc))
            return 1
        live = trial.is_real(path)
        print("Trial of {} with {}={}: the key {} of {} is switched on and off on random days "
              "(04:00 to 04:00), from tomorrow, for up to {} days.".format(
                  m["id"], name, value, ".".join(m["setting"]["path"]), path, m["max_calendar_days"]))
        if live:
            print("This is a live trial: it changes your Claude Code user settings file, and a scheduled task "
                  "({}) runs `{} -B -m hive tick` in {} at 04:00 and at logon until the trial ends.".format(
                      schedule.TASK_NAME, schedule.task_python(), schedule.package_parent()))
        if not args.yes and input("Type yes to start: ").strip().lower() != "yes":
            print("Not started.")
            return 1
        try:
            t, plan = trial.start(m["id"], value, eligibility=elig, replay_result=rp)
        except trial.TrialError as exc:
            print("Not started: {}".format(exc))
            return 1
        print("Trial file written; first trial day {}.".format(t["start_date"]))
        if plan["created"]:
            print("Scheduled task {} created.".format(schedule.TASK_NAME))
        elif plan.get("cron"):
            print("Add these lines with crontab -e:" + "\n  ".join([""] + plan["cron"]))
        else:
            print("Scheduled task (dry run, nothing created): " + " ".join(plan["create"]))
    elif args.action == "status":
        s = trial.status()
        print("No trial is running." if s is None else json.dumps(s, indent=2, sort_keys=True))
    elif args.action == "stop":
        v = trial.stop(args.reason or "other", dirs=_dirs(args))
        print("Trial stopped and the setting restored. Verdict: {}".format(v))
    return 0


def cmd_tick(args):
    try:
        out = trial.tick(dirs=_dirs(args))
    except Exception as exc:
        trial.tick_error(exc)
        raise
    print(json.dumps(out, sort_keys=True))


def cmd_keep(args):
    print("Kept on: " + ", ".join(k["id"] for k in trial.keep(args.method)))


def cmd_replay(args):
    m = catalog.load(args.method)
    if m["replay"] != "bash_output_limit":
        sys.exit("{} has no replay".format(args.method))
    print(replay.render(replay.run(_dirs(args), catalog.param(m)[1])))


def cmd_suggest(args):
    rows = store.read_jsonl(store.data_dir() / "sessions.jsonl")
    print(suggest.render(*suggest.suggest(rows, _dirs(args))))


def cmd_record(args):
    done = trial.finished()
    if args.trial:
        done = [t for t in done if t["trial_id"] == args.trial]
    if not done:
        sys.exit("No finished trial.")
    rows = store.read_jsonl(store.data_dir() / "sessions.jsonl")
    kept = store.read_json(store.data_dir() / "kept.json", []) or []
    cfg = store.read_json(store.data_dir() / "config.json", {}) or {}
    rec = record.build(done[-1], rows, kept, cfg.get("plan", "unknown"))
    print("Record written: {}".format(record.write(rec)))


def cmd_share(args):
    d = store.data_dir() / "records"
    path = Path(args.path) if args.path else max(d.glob("*.json"), key=lambda p: p.stat().st_mtime, default=None)
    if path is None:
        sys.exit("No record.")
    text, ok = record.share_text(path)
    print(text)
    return 0 if ok else 1


def cmd_validate(args):
    bad = 0
    for p in args.files:
        data = sys.stdin.buffer.read() if p == "-" else Path(p).read_bytes()
        problems = record.validate_bytes(data)
        print("{}: {}".format(p, "valid" if not problems else "INVALID"))
        for x in problems:
            print("  " + x)
        bad += 1 if problems else 0
    return 1 if bad else 0


def cmd_baseline(args):
    rows = store.read_jsonl(store.data_dir() / "sessions.jsonl")
    print(baseline.render(baseline.baseline(rows, args.method)))


def cmd_simulate(args):
    from . import simulate
    m = catalog.load(args.method)
    limit = m["max_calendar_days"]
    if args.own:
        if args.kind != "power":
            sys.exit("--own works with simulate power")
        if args.component and m["component"] is None:
            sys.exit("{} has no component".format(m["id"]))
        rows = store.read_jsonl(store.data_dir() / "sessions.jsonl")
        b = baseline.baseline(rows, m["id"])
        pool = list(baseline.day_pool(rows, m).values())
        if len(pool) < 2:
            sys.exit("fewer than 2 active days in the last {} days; run hive sense first".format(baseline.PERIOD_DAYS))
        active = b["active_share"] if args.active is None else args.active
        print(simulate.power_table_own(pool, active, m["own_guard"], args.trials, args.component, args.ratio or (),
                                       max_days=limit))
        return 0
    active = 1.0 if args.active is None else args.active
    if args.kind == "null":
        print(simulate.null_table(args.trials, args.days, args.sigma, active, max_days=limit))
    else:
        print(simulate.power_table(args.trials, args.sigma, active, max_days=limit))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hive", description="Hive {}: measure, try, judge, share.".format(
        __version__))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        p.add_argument("--dir", action="append", help="Claude Code log directory (repeatable)")
        return p

    p = add("report", cmd_report, "where the tokens of the last N days went (read only, writes nothing)")
    p.add_argument("--days", type=int, default=28, help="sessions that started in the last N days (default 28)")
    p.add_argument("--names", action="store_true", help="add project and MCP server names (local names)")
    add("sense", cmd_sense, "read the logs and update sessions.jsonl")
    add("note", cmd_note, "save a note that something went wrong (kept local)").add_argument("text")
    p = add("config", cmd_config, "set the plan or the settings file a trial changes")
    p.add_argument("--plan", choices=record.PLANS)
    p.add_argument("--settings", help="settings file a trial changes (default: the user settings file)")
    p = add("trial", cmd_trial, "start, show or stop a trial")
    p.add_argument("action", choices=("start", "status", "stop"))
    p.add_argument("method", nargs="?")
    p.add_argument("--value")
    p.add_argument("--reason", choices=("harm", "other"))
    p.add_argument("--yes", action="store_true")
    add("tick", cmd_tick, "apply the trial day's state and run the sensor (the scheduled task runs this)")
    add("keep", cmd_keep, "keep a method on after the verdict saves").add_argument("method")
    add("replay", cmd_replay, "replay a method on the last 28 days of logs").add_argument("method")
    add("suggest", cmd_suggest, "eligible methods, replay estimates and the hive's results")
    add("baseline", cmd_baseline, "active days, 04:00 boundary, per-day spread and guard rates of the last 28 "
        "days").add_argument("--method", default="bash-output-limit")
    add("record", cmd_record, "write the record of the last finished trial").add_argument("--trial")
    add("share", cmd_share, "print a record's exact bytes for posting by hand").add_argument("path", nargs="?")
    add("validate", cmd_validate, "check record files against the schema").add_argument("files", nargs="+")
    p = add("simulate", cmd_simulate, "simulated trials of the verdict")
    p.add_argument("kind", choices=("null", "power"))
    p.add_argument("--trials", type=int, default=1000)
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--sigma", type=float, default=0.5)
    p.add_argument("--active", type=float, default=None,
                   help="probability that a trial day is active (default 1.0; with --own the node's share)")
    p.add_argument("--own", action="store_true", help="draw days from the node's own last 28 days (power only)")
    p.add_argument("--component", action="store_true", help="with --own: the method's component instead of cost")
    p.add_argument("--method", default="bash-output-limit",
                   help="the catalog method: its max_calendar_days, and with --own its days and own guard")
    p.add_argument("--ratio", type=float, action="append", help="with --own: an extra true ratio (repeatable)")
    args = ap.parse_args(argv)
    if args.cmd == "trial" and args.action == "start" and (not args.method or args.value is None):
        ap.error("trial start needs a method and --value")
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="replace")
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
