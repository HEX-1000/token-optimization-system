"""Tests of `hive report` and the weights file api-v2. Run from the folder that holds hive/:

    python -B -m unittest discover -s hive/tests -t . -v

Every log is synthetic (hive/tests/logs.py) and dated relative to today, because the report reads the last
N days. The data folder is a temporary folder (HIVE_HOME) that the report must not create.
"""
import contextlib
import io
import math
import os
import statistics
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hive import __main__ as cli
from hive import breakdown, report, sense, store, weights
from hive.tests.logs import CHAIN3_COST, OPUS, SONNET, Log, chain3, tool_use, use

CANARY = "zqxCANARYqz"
TODAY = datetime.now(timezone.utc).date()


def start(days_ago=1, hour=10):
    return (datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time())
            + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M:%S")


def mcp_session(cwd, sid_text="", model=OPUS):
    """A session with an MCP tool, a tool definition shape, an attachment and a Read and a Bash call."""
    log = Log(start(), cwd)
    server = "srv" + sid_text
    log.push({"type": "api-request-shape", "shapeHash": "h1", "shape": {
        "system": [{"type": "text", "text": "system " + sid_text}],
        "tools": [{"name": "Read", "description": "read", "input_schema": {}},
                  {"name": "mcp__" + server + "__search", "description": "search " + sid_text, "input_schema": {}}]}})
    u = log.user("please look at " + sid_text)
    log.push({"type": "attachment", "parentUuid": u, "attachment": {"type": "att" + sid_text},
              "rendered": [{"content": "attached " + sid_text}]})
    a = log.call("m1" + sid_text, [tool_use("t1" + sid_text, "Read", {"file_path": "C:/" + sid_text + "/notes.md"})],
                 use(cw=5000, out=20), u, model=model)
    r = log.result("t1" + sid_text, "file text " + sid_text * 20, a[-1])
    a = log.call("m2" + sid_text, [tool_use("t2" + sid_text, "mcp__" + server + "__search", {"q": sid_text})],
                 use(cw=400, cr=5000, out=15), r, model=model)
    r = log.result("t2" + sid_text, "found " + sid_text, a[-1])
    a = log.call("m3" + sid_text, [tool_use("t3" + sid_text, "Bash", {"command": "cat " + sid_text + ".txt"})],
                 use(cw=300, cr=5400, out=10), r, model=model)
    r = log.result("t3" + sid_text, sid_text + " output", a[-1])
    log.call("m4" + sid_text, [{"type": "text", "text": "I read " + sid_text}], use(cw=50, cr=5700, out=5), r,
             model=model)
    return log


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.home = Path(self.tmp.name) / "hive"
        self.saved = os.environ.get(store.ENV)
        os.environ[store.ENV] = str(self.home)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop(store.ENV, None)
        else:
            os.environ[store.ENV] = self.saved
        self.tmp.cleanup()

    def cli(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["report", "--dir", str(self.root)] + list(extra))
        return out.getvalue(), code

    def data(self):
        """(sessions, summary) under the report's weights, as report.run builds them."""
        breakdown.use_weights(report.WEIGHTS)
        try:
            ss = breakdown.load_sessions([self.root], (TODAY - timedelta(days=report.PERIOD_DAYS)).isoformat())
            return ss, report.summarize(ss)
        finally:
            breakdown.use_weights(weights.DEFAULT)


class Totals(Base):
    def setUp(self):
        super().setUp()
        chain3(start=start(2)).write(self.root, folder="P", name="s1")
        sub = chain3(start=start(2, 11))
        for r in sub.recs:
            r["isSidechain"] = True
        sub.write(self.root, folder="P/s1/subagents", name="agent-1")
        mcp_session("D:\\Work\\Other", "abc", model=SONNET).write(self.root, folder="Q", name="s2")
        log = Log(start(3), "D:\\Work\\Old")
        u = log.user("go")
        log.call("o1", [{"type": "text", "text": "hi"}], use(inp=1000, cw=2000, cr=500, out=100, cw1=500), u,
                 model="claude-opus-4-8")
        log.call("o2", [{"type": "text", "text": "x"}], use(inp=10, out=10), u, model="gateway-model-7")
        log.write(self.root, folder="R", name="s3")

    def test_every_table_total_equals_the_loader_total(self):
        ss, d = self.data()
        raw = sum(s.sum_context + s.sum_out for s in ss)
        cost = sum(s.weighted_calls for s in ss)
        tokens = sum(sum(s.totals.values()) for s in ss)
        rows_raw = sum(v[0] + v[1] + v[2] for v in d["rows"].values())
        rows_w = sum(v[3] for v in d["rows"].values())
        self.assertTrue(math.isclose(rows_raw, raw, rel_tol=1e-9))
        self.assertTrue(math.isclose(rows_w, cost, rel_tol=1e-9))
        self.assertTrue(math.isclose(sum(v[1] for v in d["models"].values()), cost, rel_tol=1e-9))
        self.assertEqual(sum(v[0] for v in d["models"].values()), sum(s.calls for s in ss))
        self.assertEqual(sum(t for t, _w in d["kinds"]), tokens)
        self.assertTrue(math.isclose(sum(w for _t, w in d["kinds"]), cost, rel_tol=1e-9))
        self.assertEqual((d["main"], d["subagent"], d["calls"]), (3, 1, 3 + 3 + 4 + 2))

    def test_chain3_cost_by_hand_and_opus_4_8_is_priced_by_api_v2(self):
        _ss, d = self.data()
        calls, w, keys = d["models"][OPUS]
        self.assertEqual((calls, keys), (6, {OPUS}))
        self.assertTrue(math.isclose(w, 2 * CHAIN3_COST, rel_tol=1e-9))
        calls, w, keys = d["models"]["claude-opus-4-8"]
        # 1,000 input at 5, 1,500 5m writes at 6.25, 500 1h writes at 10, 500 reads at 0.50, 100 output at 25
        self.assertTrue(math.isclose(w, (1000 * 5 + 1500 * 6.25 + 500 * 10 + 500 * 0.5 + 100 * 25) / 1e6,
                                     rel_tol=1e-9))
        self.assertEqual(keys, {"claude-opus-4-8"})
        self.assertEqual(d["unpriced"], {report.OTHER_MODEL: 1})
        self.assertEqual(d["models"][report.OTHER_MODEL][2], {None})

    def test_printed_totals_and_the_unpriced_line(self):
        text, code = self.cli()
        self.assertEqual(code, 0)
        cost = sum(s.weighted_calls for s in self.data()[0])
        self.assertIn("weighted {})".format(report.fmt_usd(cost)), text)
        self.assertIn("Calls without a price: 1 (other model id 1)", text)
        self.assertIn("Every weighted total above leaves out the calls without a price.", text)
        self.assertIn("Weights: api-v2.", text)
        for head in ("By source", "Compartments", "By model", "By token kind", "Context", "Checks"):
            self.assertIn("\n" + head, "\n" + text)
        self.assertNotIn(" per day", text)

    def test_the_run_restores_the_default_weights(self):
        before = dict(breakdown.PRICES)
        self.cli()
        self.assertEqual(breakdown.WEIGHTS, weights.DEFAULT)
        self.assertEqual(breakdown.PRICES, before)


class Context(Base):
    def test_s_and_the_median_of_the_largest_context(self):
        log = Log(start())
        u = log.user("go")
        p = u
        for i, c in enumerate((100_000, 250_000, 260_000)):
            p = log.call("c%d" % i, [{"type": "text", "text": "x"}], use(cr=c, out=0), p)[-1]
        log.write(self.root, name="s1")
        chain3(start=start(1, 12)).write(self.root, name="s2")
        harness = chain3(start=start(1, 14), entrypoint=sense.SDK_CLI)
        harness.write(self.root, name="s3")
        _ss, d = self.data()
        # By hand: 100,000; 250,000 passes 200,000 (added twice: once, then 73,710); 260,000 - 176,290
        added = 100_000 + 250_000 + 73_710 + 83_710
        total = 610_000 + 31_130                                     # chain3 never passes 200,000
        self.assertTrue(math.isclose(d["s"][200_000], 1 - (added + 31_130) / total, rel_tol=1e-12))
        self.assertEqual(d["s"][400_000], 0.0)
        self.assertEqual(d["max_context_median"], statistics.median([260_000, 10_600, 10_600]))
        self.assertEqual(d["main_with_calls"], 3)


class Privacy(Base):
    def setUp(self):
        super().setUp()
        cwd = "D:\\Work\\" + CANARY + " Project"
        sid = CANARY + "-0001"
        mcp_session(cwd, CANARY).write(self.root, folder="D--Work-" + CANARY, name=sid)
        sub = chain3(start=start(1, 11), cwd=cwd, prompt=CANARY)
        for x in sub.recs:
            x["isSidechain"] = True
        sub.write(self.root, folder="D--Work-" + CANARY + "/" + sid + "/subagents", name="agent-" + CANARY)

    def test_the_canary_is_absent_from_the_default_output(self):
        text, code = self.cli()
        self.assertEqual(code, 0)
        self.assertNotIn(CANARY.lower(), text.lower())
        self.assertNotIn(str(self.root), text)
        self.assertIn(report.MCP, text)
        self.assertNotIn("mcp:", text)

    def test_names_shows_the_project_and_the_server(self):
        text, _code = self.cli("--names")
        self.assertTrue(text.startswith("This output holds local names"))
        self.assertIn("Project " + CANARY + " Project", text)
        self.assertIn("mcp:srv" + CANARY, text)

    def test_a_model_id_is_shown_only_in_the_form_of_a_claude_id(self):
        for m in ("claude-opus-5-5", "claude-sonnet-4-5-20250929", "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                  "claude-opus-4-5@20251101"):
            self.assertEqual(report.show_model(m), m)
        for m in ("arn:aws:bedrock:us-east-1:123456789012:inference-profile/x", "acme-prod-model", CANARY,
                  "claude-" + CANARY):
            self.assertEqual(report.show_model(m), report.OTHER_MODEL)
            self.assertEqual(report.show_model(m, names=True), m)

    def test_labels_not_on_the_list_are_other(self):
        self.assertEqual(report.label("injected", "att" + CANARY), report.OTHER)
        self.assertEqual(report.label("injected", "hook_success"), "hook_success")
        self.assertEqual(report.label("tool result", "mcp:" + CANARY), report.MCP)
        self.assertEqual(report.label("prefix", "mcp:" + CANARY, names=True), "mcp:" + CANARY)

    def test_the_run_creates_no_file(self):
        before = sorted(p for p in Path(self.tmp.name).rglob("*"))
        with mock.patch.object(store, "data_dir", side_effect=AssertionError("data_dir called")):
            _text, code = self.cli("--names")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p for p in Path(self.tmp.name).rglob("*")), before)
        self.assertFalse(self.home.exists())


class NoLog(Base):
    def test_no_log_names_the_folders_and_exits_1(self):
        self.root.mkdir(parents=True)
        text, code = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("No Claude Code session log found", text)
        self.assertIn(str(self.root), text)
        missing = Path(self.tmp.name) / "nowhere"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["report", "--dir", str(missing)])
        self.assertEqual(code, 1)
        self.assertIn(str(missing), out.getvalue())

    def test_logs_older_than_the_period_exit_1(self):
        chain3(start=start(40)).write(self.root, name="old")
        text, code = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("No Claude Code session with a model call started on", text)
        text, code = self.cli("--days", "60")
        self.assertEqual(code, 0)

    def test_default_folders_are_those_of_hive_sense(self):
        home = Path(self.tmp.name) / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        with mock.patch.object(Path, "home", return_value=home):
            found = [d for d in report.candidate_dirs() if d.is_dir()]
            self.assertEqual(found, sense.default_dirs())
            text, code = report.run(None)
        self.assertEqual(code, 1)
        self.assertIn(str(home / ".config" / "claude" / "projects"), text)


class WeightsV2(unittest.TestCase):
    def test_api_v1_entries_are_unchanged_in_api_v2(self):
        v1, v2 = weights.load("api-v1"), weights.load("api-v2")
        for model, row in v1["models"].items():
            self.assertEqual(v2["models"][model], row)
        added = set(v2["models"]) - set(v1["models"])
        self.assertEqual(added, set(v2["added"]["models"]))
        self.assertEqual(v2["added"]["date"], "2026-11-22")
        self.assertEqual(weights.DEFAULT, "api-v1")

    def test_price_key_agrees_with_price_for(self):
        breakdown.use_weights("api-v2")
        try:
            for m in ("claude-opus-5-5", "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001", "claude-fable-5",
                      "claude-fable-5-2", "claude-opus-5", "gateway-model-7", None, 7):
                key = report.price_key(m)
                p = breakdown.price_for(m)
                self.assertEqual(key is None, p is None, m)
                if key is not None:
                    self.assertIs(breakdown.PRICES[key], p)
            self.assertEqual(report.price_key("claude-fable-5-2"), "claude-fable-5-2")
            self.assertEqual(report.price_key("claude-sonnet-4-5-20250929"), "claude-sonnet-4-5")
        finally:
            breakdown.use_weights(weights.DEFAULT)


if __name__ == "__main__":
    unittest.main()
