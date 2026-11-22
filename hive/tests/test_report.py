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


def mark(log, uuids, **fields):
    """Add top-level fields (effort, perTurnEffort) to the records with these uuids."""
    for r in log.recs:
        if r["uuid"] in uuids:
            r.update(fields)


def text(t):
    return [{"type": "text", "text": t}]


def section(out, title):
    """The rows of a printed table below its column heads, split on spaces, up to the next empty line."""
    return [ln.split() for ln in out.split("\n" + title)[1].split("\n\n")[0].splitlines()[2:]]


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

    def assert_thinking_total_is_the_source_row(self):
        """The thinking table's totals add up to the thinking row of the source table, computed and printed."""
        _ss, d = self.data()
        row = d["rows"][("thinking", "-")][3]
        self.assertTrue(math.isclose(sum(v[3] for v in d["thinking"].values()), row, rel_tol=1e-9, abs_tol=1e-15))
        for v in d["thinking"].values():
            self.assertLessEqual(v[2], v[3] + 1e-15)                  # later context is never negative
        out, code = self.cli()
        self.assertEqual(code, 0)
        total = next(r for r in section(out, "Thinking by model") if r[0] == "TOTAL")
        self.assertEqual(total[5], report.fmt_usd(row))
        source = next(ln.split() for ln in out.splitlines() if ln.split()[:1] == ["thinking"])
        self.assertEqual(source[6], total[5])                         # the weighted column of the source table
        return d, out


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
        log.call("o1", [{"type": "text", "text": "hi"}], use(inp=1000, cw=2000, cr=500, out=100, think=40, cw1=500),
                 u, model="claude-opus-4-8")
        log.call("o2", [{"type": "text", "text": "x"}], use(inp=10, out=10, think=4), u, model="gateway-model-7")
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

    def test_thinking_total_with_an_unpriced_model(self):
        d, out = self.assert_thinking_total_is_the_source_row()
        row = next(r for r in section(out, "Thinking by model") if r[:3] == report.OTHER_MODEL.split())
        self.assertEqual(row[3:], ["1", "4", "-", "-", "-", "-", "-"])
        self.assertEqual(d["thinking"][report.OTHER_MODEL][4], {None})

    def test_the_run_restores_the_default_weights(self):
        before = dict(breakdown.PRICES)
        self.cli()
        self.assertEqual(breakdown.WEIGHTS, weights.DEFAULT)
        self.assertEqual(breakdown.PRICES, before)


class Thinking(Base):
    """Opus 5.5: k1 (2 records, the level only on the second) xhigh, k2 perTurnEffort only xhigh, k3 medium,
    k4 no level and no thinking count, plus chain3 without levels. Sonnet 5.5: j1 medium, j2 max with more
    thinking than output. The Opus file is also copied into a later file, whose calls are not counted."""

    def setUp(self):
        super().setUp()
        log = Log(start(2))
        u = log.user("go")
        k1 = log.call("k1", [{"type": "thinking", "thinking": ""}] + text("a"), use(cw=1000, out=300, think=200), u)
        mark(log, k1[1:], effort="xhigh")
        k2 = log.call("k2", text("b"), use(cr=1000, out=100, think=50), k1[-1])
        mark(log, k2, perTurnEffort="xhigh")
        k3 = log.call("k3", text("c"), use(cr=1050, out=40), k2[-1])
        mark(log, k3, effort="medium", perTurnEffort="medium")
        no_count = use(cr=1100, out=10)
        del no_count["output_tokens_details"]
        log.call("k4", text("d"), no_count, k3[-1])
        log.write(self.root, folder="P", name="s1")
        log.write(self.root, folder="P", name="s1-copy")
        chain3(start=start(2, 12)).write(self.root, folder="P", name="s2")
        log = Log(start(1))
        u = log.user("go")
        j1 = log.call("j1", text("e"), use(cw=500, out=500, think=400), u, model=SONNET)
        mark(log, j1, effort="medium")
        j2 = log.call("j2", text("f"), use(cr=500, out=100, think=150), j1[-1], model=SONNET)
        mark(log, j2, effort="max")
        log.write(self.root, folder="Q", name="s3")

    def test_thinking_and_levels_by_hand(self):
        _ss, d = self.data()
        self.assertEqual(d["repeated"], 4)
        calls, tokens, cost, total, keys = d["thinking"][OPUS]
        self.assertEqual((calls, tokens, keys), (7, 250, {OPUS}))
        self.assertTrue(math.isclose(cost, 250 * 20 / 1e6, rel_tol=1e-12))     # Opus 5.5 output: 20 per million
        self.assertGreater(total, cost)                                         # k1's block is read by k3 and k4
        calls, tokens, cost, total, keys = d["thinking"][SONNET]
        self.assertEqual((calls, tokens, keys), (2, 500, {SONNET}))             # j2's 150 capped at its output
        self.assertTrue(math.isclose(cost, 500 * 10 / 1e6, rel_tol=1e-12))
        self.assertEqual(d["levels"], {(OPUS, "xhigh"): [2, 250], (OPUS, "medium"): [1, 0],
                                       (OPUS, report.NOT_LOGGED): [4, 0], (SONNET, "medium"): [1, 400],
                                       (SONNET, "max"): [1, 100]})
        self.assertEqual(d["no_think_count"], 1)

    def test_levels_add_up_to_the_counted_calls(self):
        ss, d = self.data()
        self.assertEqual(sum(v[0] for v in d["levels"].values()), d["calls"])
        self.assertEqual(sum(v[0] for v in d["thinking"].values()), d["calls"])
        think = sum(min(c.u["think"], c.u["out"]) for s in ss for c in s.fd.calls if c.counted)
        self.assertEqual(sum(v[1] for v in d["levels"].values()), think)
        self.assertEqual(sum(v[1] for v in d["thinking"].values()), think)
        self.assertEqual({m for m, _l in d["levels"]}, set(d["models"]))

    def test_printed_lines(self):
        out, code = self.cli()
        self.assertEqual(code, 0)
        for head in ("Thinking by model", "Effort levels in the logs"):
            self.assertIn("\n" + head, out)
        self.assertLess(out.index("By token kind"), out.index("Thinking by model"))
        self.assertLess(out.index("Effort levels in the logs"), out.index("\nContext"))
        self.assertIn("\nThinking by model (counted calls; total = the thinking row of the source table)\n", out)
        self.assertIn("It is not a saving", out)
        self.assertIn("not written to the cache and not read again", out)
        self.assertIn("after a thinking drop (Checks counts them)", out)
        self.assertIn("is not the effect of a level", out)
        self.assertIn("Calls without a thinking token count in the log: 1; their thinking is counted as 0.", out)
        self.assertEqual(section(out, "Effort levels in the logs")[:-1], [         # Opus first: it costs more
            [OPUS, "medium", "1", "0"], [OPUS, "xhigh", "2", "125"], [OPUS, "not", "logged", "4", "0"],
            [SONNET, "medium", "1", "400"], [SONNET, "max", "1", "100"]])
        rows = section(out, "Thinking by model")
        d = self.data()[1]
        _calls, _tokens, output, total, _keys = d["thinking"][OPUS]
        share = "{:.1f}%".format(100.0 * total / d["models"][OPUS][1])
        all_share = "{:.1f}%".format(100.0 * total / sum(v[1] for v in d["models"].values()))
        self.assertEqual(rows[0], [OPUS, "7", "250", report.fmt_usd(output), report.fmt_usd(total - output),
                                   report.fmt_usd(total), share, all_share])
        self.assertEqual(rows[1][:3], [SONNET, "2", "500"])
        self.assertEqual(rows[2][:3], ["TOTAL", "9", "750"])

    def test_thinking_total_is_the_source_row(self):
        self.assert_thinking_total_is_the_source_row()


class LaterContext(Base):
    """Opus 5.5, 1-hour writes. h1 (C 1,000) thinks 200 of its 300 output tokens; h2 (C 1,300) writes h1's
    output; a thinking_drop event comes before h3 (C 1,350), which reads it; a compaction comes before h4,
    which starts a new context without it. A second thinking_drop is at h4 and a thinking_stripped after
    h4, the last call: neither is inside a context."""

    def setUp(self):
        super().setUp()
        log = Log(start(1))
        u = log.user("go")
        h1 = log.call("h1", [{"type": "thinking", "thinking": ""}] + text("a"), use(cw=1000, out=300, think=200), u)
        h2 = log.call("h2", text("b"), use(cr=1000, cw=300, out=50), h1[-1])
        drop = {"type": "thinking_drop", "newlyDropped": {"blockCount": 1, "turnCount": 1}}
        t = log.push({"type": "attachment", "parentUuid": h2[-1], "attachment": drop})
        log.call("h3", text("c"), use(cr=1300, cw=50, out=10), t)
        b = log.boundary()
        s = log.user("summary", parent=b, isCompactSummary=True)
        t = log.push({"type": "attachment", "parentUuid": s, "attachment": dict(drop)})
        h4 = log.call("h4", text("d"), use(cw=500, out=5), t)
        log.push({"type": "attachment", "parentUuid": h4[-1], "attachment": {"type": "thinking_stripped"}})
        log.write(self.root, name="s1")

    def test_later_context_by_hand(self):
        _ss, d = self.data()
        calls, tokens, output, total, _keys = d["thinking"][OPUS]
        self.assertEqual((calls, tokens), (4, 200))
        self.assertTrue(math.isclose(output, 200 * 20 / 1e6, rel_tol=1e-12))
        # written once at h2 (8 per million), read once at h3 (0.20 per million), not in h4's context
        self.assertTrue(math.isclose(total - output, 200 * (8 + 0.2) / 1e6, rel_tol=1e-12))
        self.assert_thinking_total_is_the_source_row()

    def test_only_the_drop_inside_a_context_is_counted(self):
        ss, d = self.data()
        self.assertEqual([e[:2] for e in ss[0].fd.thinking_events],
                         [(2, "thinking_drop"), (3, "thinking_drop"), (4, "thinking_stripped")])
        self.assertEqual(d["thinking_drops"], 1)
        out, _code = self.cli()
        self.assertIn("  Thinking drops inside a context (thinking_drop or thinking_stripped events not at a call "
                      "that starts a context): 1\n", out)


class NoLevel(Base):
    def test_logs_without_levels_or_thinking_counts_work(self):
        log = Log(start())
        u = log.user("go")
        p = u
        for i in range(3):
            usage = use(cw=1000 * (i == 0), cr=1000 * (i > 0), out=50)
            del usage["output_tokens_details"]
            p = log.call("n%d" % i, text("x"), usage, p)[-1]
        log.write(self.root, name="s1")
        out, code = self.cli()
        self.assertEqual(code, 0)
        _ss, d = self.data()
        self.assertEqual(d["levels"], {(OPUS, report.NOT_LOGGED): [3, 0]})
        self.assertEqual(d["thinking"][OPUS][:2], [3, 0])
        self.assertIn("Calls without a thinking token count in the log: 3;", out)
        self.assertIn("starts a context): 0\n", out)
        self.assertIn("  {:<40}{:<14}".format(OPUS, report.NOT_LOGGED), out)

    def test_show_level(self):
        for level in report.LEVELS:
            self.assertEqual(report.show_level(level), level)
        self.assertEqual(report.show_level(None), report.NOT_LOGGED)
        for level in ("turbo", "", 3, "XHIGH"):
            self.assertEqual(report.show_level(level), report.OTHER_LEVEL)
            self.assertEqual(report.show_level(level, names=True), str(level))

    def test_call_fields_follows_the_loader(self):
        log = Log(start())
        u = log.user("go")
        bare = {"output_tokens": 5}
        a = log.call("m1", text("x"), bare, u)                                      # no level, no thinking count

        def m1(**extra):
            return {"type": "assistant", "message": {"id": "m1", "model": OPUS, "content": [], "usage": bare},
                    **extra}

        log.push(dict(m1(uuid=a[0], effort="low"), message={"id": "m1", "model": OPUS, "content": [],
                                                            "usage": use(out=5)}))   # a seen uuid: skipped
        log.push(m1(effort="high"))                                                 # the first level
        log.push(m1(effort="max"))                                                  # a later level: ignored
        log.push({"type": "assistant", "effort": "max", "message": {"model": "<synthetic>", "content": []}})
        log.push({"type": "user", "uuid": "shared", "message": {"role": "user", "content": "x"}})
        log.push({"type": "assistant", "uuid": "shared", "effort": "low",          # uuid of a user record
                  "message": {"id": "m9", "model": OPUS, "content": [], "usage": use(out=5)}})
        log.push({"type": "assistant", "uuid": "nomid", "perTurnEffort": "low",     # no message id: the uuid
                  "message": {"model": OPUS, "content": [], "usage": use(out=1)}})
        path = log.write(self.root, name="s1")
        self.assertEqual(report.call_fields(path), {"m1": ["high", False], "nomid": ["low", True]})


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

    def test_a_level_not_on_the_list_is_other_level(self):
        log = Log(start(1, 13), "D:\\Work\\" + CANARY + " Project")
        a = log.call("e1" + CANARY, text("x"), use(cw=100, out=5), log.user("go"))
        mark(log, a, effort=CANARY)
        log.write(self.root, folder="D--Work-" + CANARY, name=CANARY + "-0002")
        out, _code = self.cli()
        self.assertNotIn(CANARY.lower(), out.lower())
        self.assertIn(report.OTHER_LEVEL, out)
        out, _code = self.cli("--names")
        self.assertIn("  {:<40}{:<14}".format(OPUS, CANARY), out)

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
