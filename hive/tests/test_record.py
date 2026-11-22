"""Tests of the record validator: it accepts the plan's example and rejects every free string."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from hive import __main__ as cli
from hive import record

EXAMPLE = {
    "schema": "hive-record/1", "hive": "0.1.0", "verdict_code": "2", "weights": "api-v1",
    "method": {"id": "bash-output-limit", "version": 1, "params": {"limit": 8000}},
    "kept_on": [], "client": {"name": "claude-code", "version": "2.1.330"}, "os": "windows", "plan": "max5",
    "start_week": "2026-W49",
    "node_shares": {"agentic": 0.45, "model_effort": 0.2, "chat": 0.15, "vision": 0.05, "other": 0.15},
    "arms": {
        "on": {"active_days": 12, "prompts": 214, "cost_per_prompt": 0.391, "calls_per_prompt": 7.12,
               "guards": {"tool_error": 0.0412, "interrupt": 0.0312, "reread": 0.118, "reread_after_compact": 0,
                          "repeat_command": 0.0041}},
        "off": {"active_days": 11, "prompts": 198, "cost_per_prompt": 0.428, "calls_per_prompt": 6.95,
                "guards": {"tool_error": 0.0398, "interrupt": 0.0303, "reread": 0.121, "reread_after_compact": 0,
                           "repeat_command": 0.0037}}},
    "cost_ratio": {"estimate": 0.914, "low": 0.781, "high": 1.05},
    "own_guard_ratio": {"estimate": 1.11, "low": 0.58, "high": 2.36},
    "component_ratio": {"estimate": 0.62, "low": 0.41, "high": 0.93},
    "cost_per_call_ratio": {"estimate": 0.951, "low": 0.842, "high": 1.07},
    "notes": {"on": 1, "off": 0}, "replay": {"net_saving_share": 0.021, "needed_rate": 0.12},
    "verdict": "no_saving_shown", "flags": [],
}
FREE = ("Secret Project Name", "secret-project-name", "secret", "C:\\Users\\someone\\notes.md",
        "2026-11-12", "2026-11-12T10:00:00Z", "zqxCANARYqz")


def leaves(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, path + (i,))
    else:
        yield path


def objects(obj, path=()):
    if isinstance(obj, dict):
        yield path
        for k, v in obj.items():
            yield from objects(v, path + (k,))


def put(rec, path, value):
    out = copy.deepcopy(rec)
    cur = out
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value
    return out


def get(rec, path):
    for p in path:
        rec = rec[p]
    return rec


class Validator(unittest.TestCase):
    def test_the_plan_example_is_valid(self):
        self.assertEqual(record.validate(EXAMPLE), [])

    def test_every_value_position_rejects_every_free_string(self):
        rec = copy.deepcopy(EXAMPLE)
        rec["kept_on"] = ["compact-window"]
        rec["flags"] = ["overridden"]
        rec["replay"] = {"net_saving_share": 0.021, "needed_rate": 0.12}
        self.assertEqual(record.validate(rec), [])
        n = 0
        for path in leaves(rec):
            for s in FREE:
                bad = put(rec, path, s)
                problems = record.validate(bad)
                self.assertTrue(problems, "accepted {!r} at {}".format(s, path))
                self.assertNotIn(s, " ".join(problems))
                n += 1
        self.assertGreater(n, 250)

    def test_every_object_rejects_an_extra_key_and_params_reject_other_names(self):
        for path in objects(EXAMPLE):
            bad = copy.deepcopy(EXAMPLE)
            target = get(bad, path) if path else bad
            target["secret-project"] = 1
            self.assertTrue(record.validate(bad), "extra key accepted at {}".format(path))
        bad = put(EXAMPLE, ("method", "params"), {"secret-project": 8000})
        self.assertTrue(record.validate(bad))

    def test_component_ratio_may_be_null_and_not_applied_is_a_flag(self):
        self.assertEqual(record.validate(put(EXAMPLE, ("component_ratio",), None)), [])
        self.assertEqual(record.validate(put(EXAMPLE, ("component_ratio", "low"), None)), [])
        self.assertEqual(record.validate(put(EXAMPLE, ("flags",), ["not_applied"])), [])
        self.assertTrue(record.validate(put(EXAMPLE, ("component_ratio",), 0.62)))
        bad = copy.deepcopy(EXAMPLE)
        del bad["component_ratio"]
        self.assertTrue(record.validate(bad))

    def test_cost_per_call_ratio_may_be_null_and_is_required(self):
        self.assertEqual(record.validate(put(EXAMPLE, ("cost_per_call_ratio",), None)), [])
        self.assertEqual(record.validate(put(EXAMPLE, ("cost_per_call_ratio", "estimate"), None)), [])
        self.assertTrue(record.validate(put(EXAMPLE, ("cost_per_call_ratio",), 0.951)))
        self.assertTrue(record.validate(put(EXAMPLE, ("cost_per_call_ratio", "low"), 0.84234)))
        self.assertTrue(record.validate(put(EXAMPLE, ("cost_per_call_ratio", "high"), "1.07")))
        bad = copy.deepcopy(EXAMPLE)
        del bad["cost_per_call_ratio"]
        self.assertTrue(record.validate(bad))

    def test_lists_take_only_known_values(self):
        self.assertTrue(record.validate(put(EXAMPLE, ("kept_on",), ["secret-project"])))
        self.assertTrue(record.validate(put(EXAMPLE, ("flags",), ["secret"])))
        self.assertTrue(record.validate(put(EXAMPLE, ("method", "params", "limit"), 9000)))
        self.assertTrue(record.validate(put(EXAMPLE, ("weights",), "api-v0")))     # no such weights file

    def test_numbers_are_rounded_and_typed(self):
        for path, value in ((("arms", "on", "cost_per_prompt"), 0.39123), (("arms", "on", "prompts"), 1234),
                            (("node_shares", "agentic"), 0.43), (("arms", "on", "active_days"), True),
                            (("notes", "on"), -1), (("cost_ratio", "low"), float("nan")),
                            (("arms", "on", "prompts"), 12.0)):
            self.assertTrue(record.validate(put(EXAMPLE, path, value)), path)

    def test_rounding_helpers(self):
        self.assertEqual(record.sig3(0.391234), 0.391)
        self.assertEqual(record.sig3(1234), 1230)
        self.assertEqual(record.sig3(214), 214)
        self.assertEqual(record.sig3(123456.7), 123000.0)
        self.assertEqual(record.share05(0.43), 0.45)
        self.assertEqual(record.iso_week(__import__("datetime").date(2026, 12, 2)), "2026-W49")

    def test_the_validate_command(self):
        with tempfile.TemporaryDirectory() as d:
            good, bad = Path(d) / "good.json", Path(d) / "bad.json"
            good.write_bytes(record.to_bytes(EXAMPLE))
            bad.write_text(json.dumps(put(EXAMPLE, ("plan",), "Secret Project Name")), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["validate", str(good)]), 0)
                self.assertEqual(cli.main(["validate", str(good), str(bad)]), 1)
            self.assertNotIn("Secret Project Name", out.getvalue())

    def test_shipped_records_are_valid(self):
        d = Path(record.__file__).resolve().parent.parent / "results" / "records"
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            self.assertEqual(record.validate_bytes(f.read_bytes()), [], f.name)


if __name__ == "__main__":
    unittest.main()
