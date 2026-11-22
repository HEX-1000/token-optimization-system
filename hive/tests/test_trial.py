"""Tests of the trial loop, the settings editor, the schedule, the verdict and the record of a full trial.

Every trial here runs on a temporary settings file (HIVE_CLAUDE_SETTINGS) and synthetic logs.
"""
import json
import os
import random
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import hive
from hive import record, schedule, settings, store, trial, verdict
from hive.__main__ import main as cli
from hive.tests.logs import Log, tool_use, use

CANARY = "zqxCANARYqz"
UTC = timezone.utc
ORIGINAL = ('{\n  "model": "opus",\n  "env": {\n    "DISABLE_TELEMETRY": "1"\n  },\n'
            '  "permissions": {\n    "allow": ["Bash(git status)"]\n  }\n}\n')


def at(d, hour=4, minute=30):
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root, self.home = base / "projects", base / "hive"
        self.project = base / ("Work " + CANARY)
        self.project.mkdir()
        self.settings = base / "settings.json"
        self.settings.write_text(ORIGINAL, encoding="utf-8", newline="")
        self.saved = {k: os.environ.get(k) for k in (store.ENV, trial.SETTINGS_ENV, trial.ALLOW_LIVE)}
        os.environ[store.ENV] = str(self.home)
        os.environ[trial.SETTINGS_ENV] = str(self.settings)
        os.environ.pop(trial.ALLOW_LIVE, None)
        self.rng = random.Random(7)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def rand(self, n):
        return bytes(self.rng.randrange(256) for _ in range(n))

    def session(self, d, n, cost_tokens, hour=10):
        """One interactive session on trial day d with one prompt and one call writing cost_tokens."""
        log = Log(start="{}T{:02d}:00:00".format(d.isoformat(), hour), cwd=str(self.project))
        u = log.user("question about " + CANARY)
        log.call("m%s%d" % (d.isoformat(), n), [{"type": "text", "text": "answer " + CANARY}],
                 use(cw=cost_tokens, out=50), u)
        log.write(self.root, folder="D--" + CANARY, name="%s-%s-%d" % (CANARY, d.isoformat(), n))

    def start(self, method="compact-window", value=200000, now=None):
        now = now or datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
        t, plan = trial.start(method, value, now=now, rand=self.rand, tz=UTC, eligibility=(0.5, 0.3),
                              replay_result={"replay_code": "1", "net_saving_share": 0.02, "needed_rate": 0.1,
                                             "passes": True})
        return t, plan

    def run_days(self, t, days, on_tokens=5000, off_tokens=10000, edit=None):
        first = date.fromisoformat(t["start_date"])
        out = []
        for i in range(days):
            d = first + timedelta(days=i)
            res = trial.tick(now=at(d), tz=UTC, dirs=[self.root])
            out.append(res)
            if "stopped" in res:
                return out
            state = res["state"]
            base = on_tokens if state == "on" else off_tokens
            self.session(d, 1, int(base * self.rng.uniform(0.9, 1.1)))
            if edit:
                edit(i)
        return out


class SettingsEditor(unittest.TestCase):
    def test_round_trips_give_back_the_bytes(self):
        for text in ("{}\n", ORIGINAL, '{"a":1}', '{\n\t"env": {}\n}', '{ "x" : [1, 2] , "y" : {"z": null} }'):
            for path in (["autoCompactWindow"], ["env", "BASH_MAX_OUTPUT_LENGTH"]):
                t1, created = settings.set_raw(text, path, "8000")
                self.assertEqual(json.loads(settings.get_raw(t1, path)), 8000)
                t2, _ = settings.set_raw(t1, path, '"4000"')
                self.assertEqual(settings.remove(t2, path, drop_empty_parent=created), text)

    def test_only_the_key_changes(self):
        t1, _ = settings.set_raw(ORIGINAL, ["env", "BASH_MAX_OUTPUT_LENGTH"], '"8000"')
        a, b = json.loads(ORIGINAL), json.loads(t1)
        self.assertEqual(b["env"].pop("BASH_MAX_OUTPUT_LENGTH"), "8000")
        self.assertEqual(a, b)

    def test_not_an_object_is_refused(self):
        for bad in ("[]", "", "{,}", '{"a": 1,}'):
            with self.assertRaises(ValueError):
                settings.set_raw(bad, ["a"], "1")


class Schedule(unittest.TestCase):
    def test_pairs_have_one_on_and_one_off(self):
        rng = random.Random(3)
        s = schedule.draw(35, lambda n: bytes(rng.randrange(256) for _ in range(n)))
        self.assertEqual(len(s), 35)
        for i in range(0, 34, 2):
            self.assertEqual(sorted(s[i:i + 2]), ["off", "on"])
        self.assertGreater(len({tuple(schedule.draw(10)) for _ in range(20)}), 1)     # os.urandom

    def test_the_day_starts_at_four(self):
        tz = timezone(timedelta(hours=3))
        self.assertEqual(schedule.trial_date("2026-12-02T00:59:00Z", tz), date(2026, 12, 1))   # 03:59 local
        self.assertEqual(schedule.trial_date("2026-12-02T01:00:00Z", tz), date(2026, 12, 2))   # 04:00 local

    def test_the_task_runs_for_the_user_from_the_package_folder(self):
        win = schedule.task_plan("C:\\data", python="C:\\py\\python.exe", platform="win32", user="PC\\me")
        (path, xml), = win["files"].items()
        self.assertEqual(path, os.path.join("C:\\data", schedule.TASK_NAME + ".xml"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        root = ET.fromstring(xml.split("?>", 1)[1])
        self.assertEqual(root.find("t:Triggers/t:LogonTrigger/t:UserId", ns).text, "PC\\me")
        self.assertEqual(root.find("t:Principals/t:Principal/t:UserId", ns).text, "PC\\me")
        self.assertEqual(root.find("t:Principals/t:Principal/t:LogonType", ns).text, "InteractiveToken")
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", ns).text, "LeastPrivilege")
        self.assertEqual(root.find("t:Settings/t:StartWhenAvailable", ns).text, "true")
        self.assertEqual(root.find("t:Settings/t:Enabled", ns).text, "true")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", ns).text, "-B -m hive tick")
        workdir = root.find("t:Actions/t:Exec/t:WorkingDirectory", ns).text
        self.assertEqual(workdir, os.path.dirname(os.path.dirname(os.path.abspath(hive.__file__))))
        self.assertTrue((Path(workdir) / "hive" / "__main__.py").is_file())
        off = schedule.task_plan("C:\\data", python="C:\\py\\python.exe", platform="win32", user="u", enabled=False)
        self.assertIn("<Enabled>false</Enabled>", next(iter(off["files"].values())))
        self.assertEqual(win["create"][:3], ["schtasks", "/Create", "/TN"])
        unix = schedule.task_plan("/data", python="/usr/bin/python3", platform="linux")
        self.assertIsNone(unix["create"])
        self.assertTrue(unix["cron"][0].startswith("0 4 * * * "))
        self.assertTrue(unix["cron"][1].startswith("@reboot"))
        self.assertIn("-B -m hive tick", unix["cron"][0])

    def test_the_task_uses_pythonw_when_it_is_there(self):
        with tempfile.TemporaryDirectory() as d:
            py = os.path.join(d, "python.exe")
            open(py, "w").close()
            self.assertEqual(schedule.task_python(py), py)
            open(os.path.join(d, "pythonw.exe"), "w").close()
            self.assertEqual(schedule.task_python(py), os.path.join(d, "pythonw.exe"))
            self.assertEqual(schedule.task_python("/usr/bin/python3"), "/usr/bin/python3")


class Verdict(unittest.TestCase):
    def days(self, n_on, n_off, ratio=1.0, guard_on=None, guard_off=None, noise=0.0, seed=1, notes_on=(),
             notes_off=()):
        """Active periods in pairs: pair i holds on day 2i (if i < n_on) and off day 2i + 1 (if i < n_off).
        Cost 10 * ratio (on) or 10 (off) times a uniform noise; the own guard has 20 opportunities a day
        and 0, 1 or 2 events unless given; notes_on and notes_off list the pairs whose period has 1 note."""
        rng = random.Random(seed)
        out = []
        for i in range(max(n_on, n_off)):
            for arm, n, idx, noted in (("on", n_on, 2 * i, notes_on), ("off", n_off, 2 * i + 1, notes_off)):
                if i >= n:
                    continue
                f = (ratio if arm == "on" else 1.0) * (1 + rng.uniform(-noise, noise))
                g = guard_on if arm == "on" else guard_off
                g = list(g) if g is not None else [rng.choice((0, 1, 2)), 20]
                out.append({"arm": arm, "day": idx, "cost": 10 * f, "prompts": 10, "calls": 70,
                            "guards": {"own": g}, "notes": 1 if i in noted else 0})
        return out

    def brute(self, days, r, seed, draws=verdict.DRAWS):
        """(p too high, p too low) of the ratio r, by dividing the on costs by r and comparing every draw
        directly; for checking the crossings."""
        rows = verdict.pairs([dict(d, cost=d["cost"] / r if d["arm"] == "on" else d["cost"]) for d in days], "own")
        pats = verdict.patterns(len(rows), seed, draws)

        def ratio(pat):
            on = off = pon = poff = 0
            for j, row in enumerate(rows):
                a, b = (row[verdict.OFF:], row[:verdict.OFF]) if pat >> j & 1 else (row[:verdict.OFF], row[verdict.OFF:])
                on, pon, off, poff = on + a[0], pon + a[1], off + b[0], poff + b[1]
            return (on / pon) / (off / poff)
        vals = [ratio(p) for p in pats]
        return (sum(v <= vals[0] for v in vals) / len(vals), sum(v >= vals[0] for v in vals) / len(vals))

    def test_verdicts_first_match_wins(self):
        j = verdict.judge
        self.assertEqual(j(self.days(9, 12, 0.5, noise=0.1), "own", 1)["verdict"], "too_little_data")
        self.assertEqual(j(self.days(10, 10, 0.5, noise=0.1), "own", 1)["verdict"], "saves")
        self.assertEqual(j(self.days(10, 10, 1.5, noise=0.1), "own", 1)["verdict"], "costs_more")
        self.assertEqual(j(self.days(10, 10, 1.0, noise=0.5), "own", 1)["verdict"], "no_saving_shown")
        # own guard far higher in the on arm -> harm
        r = j(self.days(10, 10, 0.5, guard_on=(6, 20), noise=0.1), "own", 1)
        self.assertEqual(r["verdict"], "harm")
        self.assertGreater(r["own_guard_ratio"]["low"], 1.0)

    def test_notes_need_more_than_chance(self):
        j = verdict.judge
        # 3 notes on 3 on periods: 1 draw in 8 keeps them all on, so p is about 0.125 -> not harm
        r = j(self.days(10, 10, 0.5, noise=0.1, guard_on=(1, 20), guard_off=(1, 20), notes_on=(0, 1, 2)), "own", 1)
        self.assertEqual(r["notes"], {"on": 3, "off": 0})
        self.assertEqual(r["verdict"], "saves")
        # 6 notes on 6 on periods and none off, 10 pairs: 16 of 1024 draws keep the 6 pairs unflipped -> harm
        r = j(self.days(10, 10, 0.5, noise=0.1, guard_on=(1, 20), guard_off=(1, 20), notes_on=range(6)), "own", 1)
        self.assertEqual(r["verdict"], "harm")
        self.assertAlmostEqual(r["harm_p"], 16 / 1024)
        # the same with 9 on and 12 off periods (too few days): only the 8 draws that also keep the three
        # off-only pairs unflipped reach the real 6/9 notes per on period
        r = j(self.days(9, 12, 0.5, noise=0.1, guard_on=(1, 20), guard_off=(1, 20), notes_on=range(6)), "own", 1)
        self.assertEqual(r["verdict"], "harm")
        self.assertAlmostEqual(r["harm_p"], 8 / 4096)

    def test_the_interval_is_where_the_direct_test_turns(self):
        for seed, ratio, n in ((1, 0.7, 10), (2, 1.0, 11), (3, 1.3, 12), (4, 0.9, 15), (5, 0.5, 10)):
            d = self.days(n, n, ratio, noise=0.6, seed=seed)
            res = verdict.judge(d, "own", seed, 2000)
            low, high = res["cost_ratio"]["low"], res["cost_ratio"]["high"]
            self.assertLess(low, res["cost_ratio"]["estimate"])
            self.assertGreater(high, res["cost_ratio"]["estimate"])
            # just inside the bounds a ratio is kept, just outside it is rejected (alpha 0.05 per side)
            self.assertGreater(self.brute(d, high * (1 - 1e-7), seed, 2000)[0], verdict.ALPHA)
            self.assertLessEqual(self.brute(d, high * (1 + 1e-7), seed, 2000)[0], verdict.ALPHA)
            self.assertGreater(self.brute(d, low * (1 + 1e-7), seed, 2000)[1], verdict.ALPHA)
            self.assertLessEqual(self.brute(d, low * (1 - 1e-7), seed, 2000)[1], verdict.ALPHA)
            # the bounds agree with the direct test at r = 1
            p_high, p_low = self.brute(d, 1.0, seed, 2000)
            self.assertEqual(high < 1.0, p_high <= verdict.ALPHA)
            self.assertEqual(low > 1.0, p_low <= verdict.ALPHA)

    def test_all_draws_when_few_pairs_and_the_seed_fixes_the_rest(self):
        d10 = self.days(10, 10, 0.8, noise=0.4)
        self.assertEqual(verdict.judge(d10, "own", 1)["draws"], {"count": 1024, "exact": True})
        self.assertEqual(verdict.judge(d10, "own", 1)["cost_ratio"], verdict.judge(d10, "own", 2)["cost_ratio"])
        d15 = self.days(15, 15, 0.8, noise=0.4)
        a, b = verdict.judge(d15, "own", 42, 1000), verdict.judge(d15, "own", 42, 1000)
        self.assertEqual(a["draws"], {"count": 1001, "exact": False})
        self.assertEqual(a["cost_ratio"], b["cost_ratio"])

    def test_no_opportunities_flags_the_guard(self):
        r = verdict.judge(self.days(10, 10, 0.5, guard_on=(0, 0), guard_off=(0, 0), noise=0.1), "own", 1)
        self.assertIn("guard_undefined_draws", r["flags"])
        self.assertIsNone(r["own_guard_ratio"]["estimate"])
        self.assertEqual(r["verdict"], "saves")

    def test_no_events_in_an_arm_gives_no_guard_interval(self):
        r = verdict.judge(self.days(10, 10, 0.5, guard_on=(0, 20), guard_off=(1, 20), noise=0.1), "own", 1)
        self.assertEqual(r["own_guard_ratio"], {"estimate": 0.0, "low": None, "high": None})
        self.assertEqual(r["verdict"], "saves")


class FullTrial(Base):
    def test_a_trial_from_start_to_record(self):
        t, plan = self.start()
        self.assertTrue((self.home / "trial.json").is_file())
        self.assertEqual(self.settings.read_text(encoding="utf-8"), ORIGINAL)       # nothing changes at start
        self.assertEqual(t["start_date"], "2026-12-02")
        log = self.run_days(t, 30)
        self.assertIn("stopped", log[-1])
        self.assertEqual(len(log), 21)                                               # 20 days, 10 active per arm
        self.assertEqual(self.settings.read_text(encoding="utf-8"), ORIGINAL)       # restored byte for byte
        done, = trial.finished()
        res = done["result"]
        self.assertEqual(res["arms"]["on"]["active_days"], 10)
        self.assertEqual(res["arms"]["off"]["active_days"], 10)
        self.assertEqual(res["verdict"], "saves")
        self.assertLess(res["cost_ratio"]["high"], 1.0)
        rec = record.build(done, store.read_jsonl(self.home / "sessions.jsonl"), [], "max5")
        data = record.to_bytes(rec)
        self.assertEqual(record.validate_bytes(data), [])
        self.assertNotIn(CANARY.lower().encode(), data.lower())
        for s in store.read_jsonl(self.home / "sessions.jsonl"):
            self.assertNotIn(s["session"].encode(), data)
        self.assertEqual(rec["start_week"], "2026-W49")
        self.assertEqual(rec["method"], {"id": "compact-window", "version": 1, "params": {"window": 200000}})
        path = record.write(rec, rand=self.rand)
        text, ok = record.share_text(path)
        self.assertTrue(ok)
        self.assertIn(data.decode("ascii").rstrip("\n"), text)
        self.assertIn("GitHub user name", text)

    def test_status_shows_days_and_never_cost_or_guards(self):
        t, _ = self.start()
        self.run_days(t, 6)
        s = trial.status(tz=UTC, now=at(date(2026, 12, 7)))
        self.assertEqual(set(s), {"method", "start_date", "days_done", "max_days", "min_active_days", "active_days",
                                  "last_tick", "last_tick_ok", "tick_errors"})
        self.assertEqual(s["active_days"]["on"] + s["active_days"]["off"], 5)    # the 6th day's session is not sensed yet

    def test_another_key_changed_during_the_trial_is_kept(self):
        t, _ = self.start()

        def edit(i):
            if i == 3:
                txt = self.settings.read_text(encoding="utf-8")
                self.settings.write_text(txt.replace('"model": "opus"', '"model": "sonnet"'), encoding="utf-8",
                                         newline="")
        self.run_days(t, 30, edit=edit)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), ORIGINAL.replace('"opus"', '"sonnet"'))

    def test_the_method_key_changed_outside_is_logged_and_flagged(self):
        t, _ = self.start()

        def edit(i):
            if i == 2:
                txt = self.settings.read_text(encoding="utf-8")
                self.settings.write_text(settings.set_raw(txt, ["autoCompactWindow"], "300000")[0], encoding="utf-8",
                                         newline="")
        self.run_days(t, 30, edit=edit)
        events = [r["event"] for r in store.read_jsonl(self.home / "arm_log.jsonl")]
        self.assertIn("settings_changed", events)
        self.assertIn("settings_changed", trial.finished()[0]["result"]["flags"])

    def test_overridden_by_a_project_file_and_the_environment(self):
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "settings.local.json").write_text('{"autoCompactWindow": 500000}',
                                                                       encoding="utf-8")
        t, _ = self.start()
        first = date.fromisoformat(t["start_date"])
        self.session(first - timedelta(days=1), 1, 1000)                 # the sensor sees the project
        trial.tick(now=at(first), tz=UTC, dirs=[self.root], environ={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1"})
        where = {r.get("where") for r in store.read_jsonl(self.home / "arm_log.jsonl") if r["event"] == "overridden"}
        self.assertEqual(where, {"project", "environment"})

    def test_a_missing_settings_file_is_created_and_removed(self):
        self.settings.unlink()
        t, _ = self.start(method="bash-output-limit", value=8000)
        first = date.fromisoformat(t["start_date"])
        states = []
        for i in range(4):
            states.append(trial.tick(now=at(first + timedelta(days=i)), tz=UTC, dirs=[self.root])["state"])
            txt = settings.read(self.settings)[0]
            raw = settings.get_raw(txt, ["bashOutputMaxChars"])
            self.assertEqual(raw, "8000" if states[-1] == "on" else None)
        trial.stop("other", now=at(first + timedelta(days=4)), tz=UTC, dirs=[self.root])
        self.assertFalse(self.settings.exists())
        self.assertIn("stopped_by_user_other", trial.finished()[0]["result"]["flags"])

    def test_periods_sessions_across_a_tick_and_notes(self):
        t, _ = self.start()
        first = date.fromisoformat(t["start_date"])
        for i in range(4):
            trial.tick(now=at(first + timedelta(days=i)), tz=UTC, dirs=[self.root], run_sensor=False)
        late = at(first + timedelta(days=4), hour=9)                 # day 4 is applied late, at logon
        trial.tick(now=late, tz=UTC, dirs=[self.root], run_sensor=False)
        self.session(first, 1, 1000)                                 # day 0, 10:00
        self.session(first + timedelta(days=4), 1, 1000, hour=6)     # day 4 at 06:00, before its tick
        self.session(first + timedelta(days=2), 1, 1000, hour=23)    # day 2 at 23:00 ...
        log = Log(start="{}T23:00:00".format((first + timedelta(days=1)).isoformat()), cwd=str(self.project))
        u = log.user("long session")
        a = log.call("x1", [{"type": "text", "text": "a"}], use(cw=1000, out=5), u)
        log.call("x2", [{"type": "text", "text": "b"}], use(cw=1000, out=5), log.user("more", a[-1]))
        recs = [r for r in log.recs if "timestamp" in r]
        recs[-1]["timestamp"] = "{}T05:00:00Z".format((first + timedelta(days=2)).isoformat())
        log.write(self.root, folder="D--" + CANARY, name="span")    # day 1 23:00 to day 2 05:00: spans a tick
        from hive import sense
        sense.sense([self.root])
        notes = [{"time": "{}T12:00:00Z".format(first.isoformat())},
                 {"time": "{}T12:00:00Z".format((first + timedelta(days=1)).isoformat())}]   # day 1: no period
        days, (excluded, seen) = trial.collect(t, store.read_jsonl(self.home / "sessions.jsonl"), notes, UTC)
        self.assertEqual([d["day"] for d in days], [0, 2, 3])        # the 06:00 session belongs to day 3's state
        self.assertEqual([d["arm"] for d in days], [t["schedule"][i] for i in (0, 2, 3)])
        self.assertEqual((excluded, seen), (1, 4))
        self.assertEqual([d["notes"] for d in days], [1, 0, 0])

    def test_the_real_user_settings_file_is_refused(self):
        os.environ.pop(trial.SETTINGS_ENV)
        with self.assertRaises(trial.TrialError):
            self.start()
        self.assertFalse((self.home / "trial.json").exists())

    def test_a_changed_verdict_code_makes_no_record(self):
        t, _ = self.start()
        data = json.loads((self.home / "trial.json").read_text(encoding="utf-8"))
        data["verdict_hash"] = "0" * 64
        store.write_json(self.home / "trial.json", data)
        self.run_days(t, 30)
        done, = trial.finished()
        self.assertEqual(done["result"], {"verdict": None, "error": "verdict_code_changed"})
        with self.assertRaises(ValueError):
            record.build(done, [], [], "max5")

    def test_start_refuses_an_ineligible_method_and_a_failed_replay(self):
        with self.assertRaises(trial.TrialError):
            trial.start("compact-window", 200000, rand=self.rand, eligibility=(0.1, 0.3))
        with self.assertRaises(trial.TrialError):
            trial.start("bash-output-limit", 8000, rand=self.rand, eligibility=(0.5, 0.1),
                        replay_result={"passes": False})
        with self.assertRaises(trial.TrialError):
            trial.start("bash-output-limit", 9000, rand=self.rand, eligibility=(0.5, 0.1),
                        replay_result={"passes": True})
        self.assertFalse((self.home / "trial.json").exists())

    def test_a_client_older_than_the_setting_is_not_eligible(self):
        from hive import catalog, suggest
        m = catalog.load("bash-output-limit")
        met = {"bash_cost_share": 0.3}
        for version, ok in (("2.1.322", True), ("2.1.261", True), ("2.1.260", False), ("2.0.999", False),
                            (None, False)):
            val, lo = suggest.eligibility(m, dict(met, client_version=version), 8000)
            self.assertEqual(val is not None and val >= lo, ok, version)
        rows = [{"start": "2026-11-0{}T10:00:00Z".format(i), "cost": 1.0, "bash": {"cost": 0.5},
                 "s_sums": {"200000": [0, 0], "400000": [0, 0]}, "client_version": v}
                for i, v in ((1, "2.1.330"), (3, "2.1.250"), (2, "x"))]
        self.assertEqual(suggest.metrics(rows, date(2026, 11, 10))["client_version"], "2.1.250")   # newest session

    def bash_session(self, d, chars, hour=10):
        """One session on trial day d: a prompt, a Bash call, its successful result of `chars` characters
        (written as chars // 2 new tokens of the next call) and a final answer."""
        tag = "%s-%d" % (d.isoformat(), chars)
        log = Log(start="{}T{:02d}:00:00".format(d.isoformat(), hour), cwd=str(self.project))
        u = log.user("run the build " + CANARY)
        a = log.call("b1" + tag, [tool_use("t" + tag, "Bash", {"command": "make " + CANARY})], use(cw=5000, out=10), u)
        r = log.result("t" + tag, CANARY + "z" * chars, a[-1])
        log.call("b2" + tag, [{"type": "text", "text": "done"}], use(cw=chars // 2, cr=5000, out=5), r)
        log.write(self.root, folder="D--" + CANARY, name="%s-%s" % (CANARY, tag))

    def bash_trial(self, first_on_chars):
        t, _ = self.start(method="bash-output-limit", value=8000)
        first, seen_on = date.fromisoformat(t["start_date"]), False
        for i in range(36):
            d = first + timedelta(days=i)
            res = trial.tick(now=at(d), tz=UTC, dirs=[self.root])
            if "stopped" in res:
                break
            on = res["state"] == "on"
            chars = (first_on_chars if not seen_on else 3000) if on else 20000
            seen_on = seen_on or on
            self.bash_session(d, chars + self.rng.randrange(100))
        done, = trial.finished()
        return t, done

    def test_component_ratio_is_reported_and_not_judged(self):
        t, done = self.bash_trial(3000)
        res = done["result"]
        c = res["component_ratio"]
        self.assertLess(c["estimate"], 0.2)                        # on days carry about a seventh of the output
        self.assertLess(c["high"], 1.0)
        self.assertLessEqual(c["low"], c["estimate"])
        self.assertNotIn("not_applied", res["flags"])
        self.assertEqual(res["sessions"]["not_applied"], 0)
        days, _ = trial.collect(t, store.read_jsonl(self.home / "sessions.jsonl"), [], UTC)
        self.assertEqual(verdict.judge(days, t["own_guard"], t["seed"])["verdict"], res["verdict"])
        rec = record.build(done, store.read_jsonl(self.home / "sessions.jsonl"), [], "max5")
        self.assertEqual(rec["component_ratio"]["high"], record.sig3(c["high"]))
        data = record.to_bytes(rec)
        self.assertEqual(record.validate_bytes(data), [])
        self.assertNotIn(CANARY.lower().encode(), data.lower())

    def test_cost_per_call_ratio_is_reported_and_not_judged(self):
        t, done = self.bash_trial(3000)
        res = done["result"]
        days, _ = trial.collect(t, store.read_jsonl(self.home / "sessions.jsonl"), [], UTC)
        on = [d for d in days if d["arm"] == "on"]
        off = [d for d in days if d["arm"] == "off"]
        per_call = (sum(d["cost"] for d in on) / sum(d["calls"] for d in on)) / \
            (sum(d["cost"] for d in off) / sum(d["calls"] for d in off))
        c = res["cost_per_call_ratio"]
        self.assertAlmostEqual(c["estimate"], per_call, places=12)
        swapped = verdict.judge([dict(d, prompts=d["calls"]) for d in days], t["own_guard"], t["seed"])
        self.assertEqual((c["low"], c["high"]), (swapped["cost_ratio"]["low"], swapped["cost_ratio"]["high"]))
        self.assertLessEqual(c["low"], c["estimate"])
        self.assertLessEqual(c["estimate"], c["high"])
        judged = verdict.judge(days, t["own_guard"], t["seed"])
        self.assertEqual((judged["verdict"], judged["cost_ratio"]), (res["verdict"], res["cost_ratio"]))
        rec = record.build(done, store.read_jsonl(self.home / "sessions.jsonl"), [], "max5")
        self.assertEqual(rec["cost_per_call_ratio"], {k: record.sig3(v) for k, v in c.items()})
        self.assertEqual(record.validate_bytes(record.to_bytes(rec)), [])

    def test_cost_per_call_ratio_sees_calls_and_not_prompts(self):
        # The same cost per prompt in both arms, twice the calls per prompt on: per prompt 1.0, per call 0.5.
        t, _ = self.start()
        days = []
        for k in range(20):
            arm = ("on", "off")[(k + k // 2) % 2]
            cost = 1.0 + 0.1 * (k // 2)
            days.append({"arm": arm, "day": k, "cost": cost, "prompts": 10, "calls": 40 if arm == "on" else 20,
                         "guards": {}, "notes": 0})
        c = trial.cost_per_call_ratio(t, days)
        self.assertAlmostEqual(c["estimate"], 0.5, places=12)
        self.assertLess(c["high"], 1.0)
        self.assertAlmostEqual(verdict.judge(days, t["own_guard"], t["seed"])["cost_ratio"]["estimate"], 1.0,
                               places=12)
        self.assertEqual(trial.cost_per_call_ratio(t, [d for d in days if d["arm"] == "on"])["estimate"], None)

    def test_a_long_result_on_an_on_day_flags_not_applied(self):
        _, done = self.bash_trial(12000)                           # above the limit of 8,000: not applied
        self.assertIn("not_applied", done["result"]["flags"])
        self.assertEqual(done["result"]["sessions"]["not_applied"], 1)
        rec = record.build(done, store.read_jsonl(self.home / "sessions.jsonl"), [], "max5")
        self.assertIn("not_applied", rec["flags"])

    def test_compact_window_has_no_component_and_checks_the_context(self):
        from hive import catalog
        t, _ = self.start()
        self.assertIsNone(trial.component_ratio(t, []))
        m = catalog.load("compact-window")
        self.assertTrue(catalog.not_applied(m, 200000, {"max_context": 250000}))
        self.assertFalse(catalog.not_applied(m, 200000, {"max_context": 150000}))
        self.assertFalse(catalog.not_applied(m, 200000, {}))       # a row written before the field existed

    def test_keep_needs_saves(self):
        t, _ = self.start()
        self.run_days(t, 30)
        kept = trial.keep("compact-window")
        self.assertEqual(kept, [{"id": "compact-window", "version": 1, "params": {"window": 200000}}])
        self.assertEqual(json.loads(settings.get_raw(self.settings.read_text(encoding="utf-8"),
                                                     ["autoCompactWindow"])), 200000)
        with self.assertRaises(trial.TrialError):
            trial.keep("bash-output-limit")


if __name__ == "__main__":
    unittest.main()


class LiveTrial(Base):
    """A live trial on the real user settings file (here a temporary file in its place) with the scheduled
    task: schtasks is replaced by a recorder, and the plan is the Windows one on every system."""

    def setUp(self):
        super().setUp()
        os.environ.pop(trial.SETTINGS_ENV)
        self.calls, self.code = [], 0
        plan = schedule.task_plan
        for patcher in (mock.patch.object(trial, "real_settings", lambda: self.settings),
                        mock.patch.object(schedule, "_run", self.schtasks),
                        mock.patch.object(schedule, "task_plan",
                                          lambda d, **kw: plan(d, **dict(kw, platform="win32", user="PC\\me")))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def schtasks(self, cmd):
        self.calls.append(cmd[:2])
        return self.code, "ERROR: Access is denied." if self.code else "SUCCESS"

    def live_start(self):
        os.environ[trial.ALLOW_LIVE] = "1"
        try:
            return self.start("bash-output-limit", 4000)
        finally:
            os.environ.pop(trial.ALLOW_LIVE)

    def test_a_live_trial_runs_without_the_variable_and_removes_its_task(self):
        t, plan = self.live_start()
        self.assertIs(t["live"], True)
        self.assertTrue(plan["created"])
        self.assertEqual(self.calls, [["schtasks", "/Create"]])
        self.assertTrue((self.home / (schedule.TASK_NAME + ".xml")).is_file())
        self.assertEqual(self.settings.read_text(encoding="utf-8"), ORIGINAL)
        log = self.run_days(t, 4)                                   # the ticks run without HIVE_ALLOW_LIVE
        self.assertTrue(all(r["ok"] for r in log))
        on = next(i for i, r in enumerate(log) if r["state"] == "on")
        self.assertEqual(trial.arm_log(t["trial_id"])[1 + on]["value_text"], "4000")
        trial.stop("other", now=at(date(2026, 12, 6)), tz=UTC, run_sensor=False)
        self.assertEqual(self.calls, [["schtasks", "/Create"], ["schtasks", "/Delete"]])
        self.assertEqual(trial.arm_log(t["trial_id"])[-1]["event"], "task_removed")
        self.assertTrue(trial.arm_log(t["trial_id"])[-1]["ok"])
        self.assertEqual(self.settings.read_text(encoding="utf-8"), ORIGINAL)

    def test_a_trial_file_without_live_cannot_change_the_real_file(self):
        t, _ = self.live_start()
        data = json.loads((self.home / "trial.json").read_text(encoding="utf-8"))
        data["live"] = False
        store.write_json(self.home / "trial.json", data)
        with self.assertRaises(trial.TrialError):
            trial.tick(now=at(date.fromisoformat(t["start_date"])), tz=UTC, run_sensor=False)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), ORIGINAL)

    def test_a_failed_task_withdraws_the_trial(self):
        self.code = 1
        with self.assertRaises(trial.TrialError) as cm:
            self.live_start()
        self.assertIn("Access is denied", str(cm.exception))
        self.assertFalse((self.home / "trial.json").exists())
        self.assertEqual([r["event"] for r in store.read_jsonl(self.home / "arm_log.jsonl")], ["start", "start_failed"])
        self.assertEqual(self.settings.read_text(encoding="utf-8"), ORIGINAL)

    def test_a_trial_on_a_test_file_creates_no_task(self):
        os.environ[trial.SETTINGS_ENV] = str(Path(self.tmp.name) / "other.json")
        t, plan = self.start("bash-output-limit", 4000)
        self.assertIs(t["live"], False)
        self.assertFalse(plan["created"])
        trial.stop("other", now=at(date(2026, 12, 2)), tz=UTC, run_sensor=False)
        self.assertEqual(self.calls, [])
        self.assertNotIn("task_removed", [r["event"] for r in trial.arm_log(t["trial_id"])])


class TickErrors(Base):
    def test_a_sensor_error_does_not_keep_the_state_from_being_applied(self):
        t, _ = self.start()
        with mock.patch("hive.sense.sense", side_effect=OSError("disk")):
            res = trial.tick(now=at(date.fromisoformat(t["start_date"])), tz=UTC, dirs=[self.root])
        self.assertTrue(res["ok"])
        self.assertEqual([r["event"] for r in trial.arm_log(t["trial_id"])], ["start", "sense_error", "applied"])
        self.assertEqual(trial.arm_log(t["trial_id"])[1]["error"], "OSError")

    def test_a_failed_tick_is_written_down_and_counted_by_status(self):
        t, _ = self.start()
        with mock.patch.object(trial, "tick", side_effect=settings.SettingsError("not an object")), \
                mock.patch.object(trial, "now_utc", lambda: datetime(2026, 11, 30, tzinfo=UTC)):
            with self.assertRaises(settings.SettingsError):
                cli(["tick", "--dir", str(self.root)])
        row, = store.read_jsonl(self.home / "tick_errors.jsonl")
        self.assertEqual(row["error"], "SettingsError: not an object")
        trial.tick_error(ValueError("x"), now=at(date(2026, 12, 2)))
        s = trial.status(tz=UTC, now=at(date(2026, 12, 2)))
        self.assertEqual(s["tick_errors"], 1)          # the row before the trial's creation does not count
        self.assertIsNone(s["last_tick"])
