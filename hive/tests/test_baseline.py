"""Tests of the node baseline (hive.baseline) and of the power simulation with the node's own days."""
import random
import unittest
from datetime import date, timezone

from hive import baseline, catalog, simulate

UTC = timezone.utc
CANARY = "zqxCANARYqz"


def row(start, end, cost, prompts, bash=0.0, repeat=(0, 0), entrypoint="cli"):
    return {"session": CANARY + start, "project": "D:\\" + CANARY, "entrypoint": entrypoint, "has_main": True,
            "main_calls": 1, "start": start, "end": end, "cost": cost, "prompts": prompts, "calls": 3 * prompts,
            "bash": {"cost": bash}, "guards": {"tool_error": [0, 4], "interrupt": [0, prompts], "reread": [1, 5],
                                               "reread_after_compact": [0, 0], "repeat_command": list(repeat)}}


ROWS = [
    row("2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z", 2.0, 4, bash=0.5, repeat=(1, 10)),
    row("2026-11-10T23:00:00Z", "2026-11-11T05:00:00Z", 6.0, 3, bash=1.0, repeat=(0, 6)),   # crosses 04:00
    row("2026-11-12T10:00:00Z", "2026-11-12T10:30:00Z", 1.0, 1),
    row("2026-11-12T12:00:00Z", "2026-11-12T12:30:00Z", 50.0, 9, entrypoint="sdk-cli"),     # not counted
    row("2026-10-01T10:00:00Z", "2026-10-01T10:30:00Z", 9.0, 2),                            # before the period
]
TODAY = date(2026, 12, 1)


class Baseline(unittest.TestCase):
    def test_period_days_boundary_and_guards(self):
        b = baseline.baseline(ROWS, "bash-output-limit", tz=UTC, today=TODAY)
        self.assertEqual((b["first_day"], b["last_day"]), ("2026-11-03", "2026-11-30"))
        self.assertEqual(b["sessions"], 3)
        self.assertEqual(b["cost"], 9.0)
        self.assertEqual(b["spanning_sessions"], 1)
        self.assertAlmostEqual(b["spanning_session_share"], 1 / 3)
        self.assertAlmostEqual(b["spanning_cost_share"], 6 / 9)
        self.assertEqual(b["active_days"], 2)                     # the crossing session is not in a day
        self.assertAlmostEqual(b["active_share"], 2 / 28)
        self.assertAlmostEqual(b["component_share"], 1.5 / 9)
        self.assertEqual(b["days_without_component"], 1)
        self.assertEqual(b["guards"]["repeat_command"], (1 / 16, 1, 16))
        self.assertEqual(b["guards"]["reread"], (3 / 15, 3, 15))
        self.assertAlmostEqual(b["cost_per_prompt"][0], 0.75)      # days of 0.5 and 1.0 per prompt
        text = baseline.render(b)
        self.assertNotIn(CANARY, text)
        self.assertIn("own guard", text)

    def test_day_pool(self):
        pool = baseline.day_pool(ROWS, catalog.load("bash-output-limit"), tz=UTC, today=TODAY)
        self.assertEqual(sorted(pool), [date(2026, 11, 10), date(2026, 11, 12)])
        d = pool[date(2026, 11, 10)]
        self.assertEqual((d["cost"], d["prompts"], d["component"], d["sessions"]), (2.0, 4, 0.5, 1))
        self.assertEqual(d["guards"]["repeat_command"], [1, 10])
        pool = baseline.day_pool(ROWS, catalog.load("compact-window"), tz=UTC, today=TODAY)
        self.assertEqual(pool[date(2026, 11, 10)]["component"], 0.0)


class OwnDays(unittest.TestCase):
    POOL = [{"cost": 2.0, "prompts": 4, "calls": 12, "component": 0.5, "guards": {"repeat_command": [1, 10]}},
            {"cost": 1.0, "prompts": 1, "calls": 3, "component": 0.0, "guards": {"repeat_command": [0, 3]}}]

    def test_days_come_from_the_pool_and_the_on_arm_is_scaled(self):
        ds = simulate.trial_days_pool(random.Random(1), 10, 0.5, self.POOL, 1.0, "component", "repeat_command")
        self.assertEqual(len(ds), 20)
        self.assertEqual(sum(1 for d in ds if d["arm"] == "on"), 10)
        for d in ds:
            src = self.POOL[0] if d["prompts"] == 4 else self.POOL[1]
            self.assertEqual(d["cost"], src["component"] * (0.5 if d["arm"] == "on" else 1.0))
            self.assertEqual(d["guards"], {"repeat_command": src["guards"]["repeat_command"]})

    def test_a_large_saving_is_seen_and_no_change_is_not(self):
        pool = [dict(self.POOL[0], cost=2.0 * k) for k in (0.9, 1.0, 1.1)]
        v, below = simulate.one_trial_pool((5, 10, 0.3, pool, 1.0, "cost", "repeat_command", 2000, None))
        self.assertEqual((v, below), ("saves", True))
        same = [dict(self.POOL[0])]
        v, below = simulate.one_trial_pool((5, 10, 1.0, same, 1.0, "cost", "repeat_command", 2000, None))
        self.assertEqual((v, below), ("no_saving_shown", False))


class MaxDays(unittest.TestCase):
    def test_the_maximum_follows_the_catalog(self):
        for mid in catalog.all_ids():
            self.assertEqual(catalog.load(mid)["max_calendar_days"], 42)
            self.assertEqual(simulate.max_days(mid), 42)
        self.assertEqual(simulate.MAX_DAYS, catalog.load(simulate.DEFAULT_METHOD)["max_calendar_days"])

    def test_a_trial_stops_at_the_maximum(self):
        rng = random.Random(3)
        pool = [OwnDays.POOL[0]]
        last = []
        for _ in range(200):                       # few active days: most trials run to the maximum
            ds = simulate.trial_days(rng, 10, 1.0, 0.5, 0.0, 0.3)
            last.append(max(d["day"] for d in ds))
            self.assertTrue(all(d["day"] < 42 for d in ds))
            self.assertTrue(all(d["day"] < 20 for d in simulate.trial_days(rng, 10, 1.0, 0.5, 0.0, 0.3, 20)))
            self.assertTrue(all(d["day"] < 20 for d in
                                simulate.trial_days_pool(rng, 10, 1.0, pool, 0.3, "cost", "repeat_command", 20)))
        self.assertEqual(max(last), 41)            # days 35 to 41 are reached; the old maximum was 35


if __name__ == "__main__":
    unittest.main()
