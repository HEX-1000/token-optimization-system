"""Tests of the sensor, the guards and the prompt counter. Run from the folder that holds hive/:

    python -B -m unittest discover -s hive/tests -t . -v

Every log is synthetic (hive/tests/logs.py). The data folder is a temporary folder (HIVE_HOME).
"""
import contextlib
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path

from hive import __main__ as cli
from hive import breakdown, sense, store
from hive.tests.logs import CHAIN3_COST, Log, chain3, tool_use, use

CANARY = "zqxCANARYqz"


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

    def rows(self):
        return store.read_jsonl(self.home / "sessions.jsonl")


class Rows(Base):
    def test_chain3_row_by_hand(self):
        chain3().write(self.root, name="s1")
        r = sense.sense([self.root], fold_case=True)
        self.assertEqual((r["sessions_on_disk"], r["rows_built"]), (1, 1))
        row, = self.rows()
        self.assertEqual(row["session"], "s1")
        self.assertEqual(row["calls"], 3)
        self.assertEqual(row["calls_by_model"], {"claude-opus-5-5": 3})
        self.assertEqual(row["tokens"], {"input": 0, "cw": 10600, "cr": 20530, "out": 55})
        self.assertTrue(math.isclose(row["cost"], CHAIN3_COST, rel_tol=1e-9))
        self.assertEqual(row["prompts"], 1)
        self.assertEqual(row["entrypoint"], "cli")
        self.assertEqual(row["client_version"], "2.1.319")
        self.assertEqual(row["weights"], "api-v1")
        self.assertEqual(row["s_sums"]["200000"], [31130, 31130])     # below the window: S = 0
        self.assertEqual(row["bash"]["results"], 1)
        self.assertEqual(row["guards"]["tool_error"], [0, 2])
        self.assertEqual(row["guards"]["interrupt"], [0, 1])

    def test_applied_check_facts_and_row_version(self):
        log = Log()
        u = log.user("go")
        a = log.call("m1", [tool_use("t1", "PowerShell", {"command": "dir"})], use(cw=10000, out=10), u)
        r = log.result("t1", "x" * 5000, a[-1])
        a = log.call("m2", [tool_use("t2", "Bash", {"command": "make"})], use(cw=2000, cr=10000, out=10), r)
        r = log.result("t2", "y" * 9000, a[-1], is_error=True)          # a failed result does not count
        log.call("m3", [{"type": "text", "text": "ok"}], use(cw=3000, cr=12000, out=5), r)
        log.write(self.root, name="s1")
        sense.sense([self.root])
        row, = self.rows()
        self.assertEqual(row["row"], sense.ROW_VERSION)
        self.assertEqual(row["shell_ok_max_chars"], 5000)
        self.assertEqual(row["max_context"], 15000)
        old = dict(row, row=1)
        old.pop("shell_ok_max_chars")
        store.write_jsonl(self.home / "sessions.jsonl", [old])
        r = sense.sense([self.root])
        self.assertEqual(r["rows_built"], 1)                               # an older row version is rebuilt
        self.assertEqual(self.rows()[0]["shell_ok_max_chars"], 5000)

    def test_fable_is_priced(self):
        log = Log()
        u = log.user("go")
        log.call("f1", [{"type": "text", "text": "hi"}], use(inp=1000, out=100), u, model="claude-fable-5-2")
        log.write(self.root, name="s1")
        sense.sense([self.root])
        row, = self.rows()
        self.assertTrue(math.isclose(row["cost"], (1000 * 8 + 100 * 40) / 1e6, rel_tol=1e-9))
        self.assertEqual(row["checks"]["unpriced_calls"], 0)

    def test_subagent_file_counts_with_its_parent(self):
        chain3().write(self.root, folder="P", name="s1")
        sub = chain3(start="2026-11-01T10:05:00")
        sub.sidechain = True
        for r in sub.recs:
            r["isSidechain"] = True
        sub.write(self.root, folder="P/s1/subagents", name="agent-1")
        sense.sense([self.root])
        row, = self.rows()
        self.assertEqual(row["subagent_files"], 1)
        self.assertEqual(row["calls"], 6)
        self.assertEqual(row["main_calls"], 3)
        self.assertEqual(row["prompts"], 1)                                   # sidechain prompts are not typed
        self.assertTrue(math.isclose(row["cost"], 2 * CHAIN3_COST, rel_tol=1e-9))

    def test_rows_of_deleted_logs_are_kept_and_unchanged_rows_are_not_rebuilt(self):
        p1 = chain3().write(self.root, name="s1")
        chain3(start="2026-11-02T10:00:00").write(self.root, name="s2")
        sense.sense([self.root])
        r = sense.sense([self.root])
        self.assertEqual((r["rows_built"], r["rows_unchanged"]), (0, 2))
        p1.unlink()
        r = sense.sense([self.root])
        self.assertEqual((r["rows"], r["rows_of_deleted_logs"]), (2, 1))
        by = {x["session"]: x for x in self.rows()}
        self.assertFalse(by["s1"]["log_present"])
        self.assertTrue(by["s2"]["log_present"])
        self.assertEqual(by["s1"]["calls"], 3)


class Guards(Base):
    def test_every_guard_by_hand(self):
        log = Log()
        u = log.user("go")
        a = log.call("m1", [tool_use("t1", "Read", {"file_path": "D:/x/a.md"})], use(cw=1000), u)
        r = log.result("t1", "text", a[-1])
        a = log.call("m2", [tool_use("t2", "Read", {"file_path": "D:\\x\\A.MD"})], use(cr=1000, cw=10), r)
        r = log.result("t2", "text", a[-1])
        a = log.call("m3", [tool_use("t3", "Bash", {"command": "ls"})], use(cr=1010, cw=10), r)
        r = log.result("t3", "error", a[-1], is_error=True)
        a = log.call("m4", [tool_use("t4", "Bash", {"command": "ls"})], use(cr=1020, cw=10), r)
        r = log.result("t4", "out", a[-1])
        b = log.boundary()
        a = log.call("m5", [tool_use("t5", "Read", {"file_path": "D:/x/a.md"})], use(cw=500), b)
        r = log.result("t5", "text", a[-1])
        a = log.call("m6", [tool_use("t6", "Read", {"file_path": "D:/x/a.md"})], use(cr=500, cw=10), r)
        r = log.result("t6", "text", a[-1])
        u2 = log.user("[Request interrupted by user]", r)
        u3 = log.user("more", u2)
        log.call("m7", [{"type": "text", "text": "ok"}], use(cr=510, cw=10), u3)
        log.write(self.root, name="s1")
        sense.sense([self.root], fold_case=True)
        g = self.rows()[0]["guards"]
        self.assertEqual(g["reread"], [3, 4])                    # t2 (case folded), t5, t6
        self.assertEqual(g["reread_after_compact"], [1, 2])      # t5: read before the compaction, not after
        self.assertEqual(g["repeat_command"], [1, 2])
        self.assertEqual(g["tool_error"], [1, 6])
        self.assertEqual(g["interrupt"], [1, 2])
        sense.sense([self.root], fold_case=False)                # rows unchanged: same files
        self.assertEqual(self.rows()[0]["guards"]["reread"], [3, 4])

    def test_case_is_kept_where_paths_are_case_sensitive(self):
        log = Log()
        u = log.user("go")
        a = log.call("m1", [tool_use("t1", "Read", {"file_path": "/x/a.md"})], use(cw=1000), u)
        r = log.result("t1", "text", a[-1])
        log.call("m2", [tool_use("t2", "Read", {"file_path": "/x/A.md"})], use(cr=1000, cw=10), r)
        log.write(self.root, name="s1")
        sense.sense([self.root], fold_case=False)
        self.assertEqual(self.rows()[0]["guards"]["reread"], [0, 2])

    def test_repeated_message_ids_count_in_the_earliest_file_only(self):
        first = chain3()
        first.write(self.root, name="s1")
        again = chain3()
        for r in again.recs:
            r["timestamp"] = r["timestamp"].replace("2026-11-01", "2026-11-03")
        again.write(self.root, name="s2")
        sense.sense([self.root])
        by = {x["session"]: x for x in self.rows()}
        self.assertEqual(by["s1"]["guards"]["tool_error"], [0, 2])
        self.assertEqual(by["s2"]["guards"]["tool_error"], [0, 0])
        self.assertEqual(by["s2"]["calls"], 0)


class Canary(Base):
    def test_the_canary_reaches_only_the_project_and_id_fields(self):
        cwd = "D:\\Work\\" + CANARY + " Project"
        log = Log(cwd=cwd)
        u = log.user("please look at " + CANARY)
        a = log.call("m1", [tool_use("t1", "Read", {"file_path": "C:/" + CANARY + "/notes.md"})], use(cw=5000), u)
        r = log.result("t1", "file text " + CANARY * 20, a[-1])
        a = log.call("m2", [tool_use("t2", "Bash", {"command": "cat " + CANARY + ".txt"})], use(cr=5000, cw=300), r)
        r = log.result("t2", CANARY + " output", a[-1], is_error=True)
        log.call("m3", [{"type": "text", "text": "I read " + CANARY}], use(cr=5300, cw=50, out=10), r)
        sid = CANARY + "-0001"
        log.write(self.root, folder="D--Work-" + CANARY, name=sid)
        sub = chain3(start="2026-11-01T10:05:00", cwd=cwd, prompt=CANARY)
        for x in sub.recs:
            x["isSidechain"] = True
        sub.write(self.root, folder="D--Work-" + CANARY + "/" + sid + "/subagents", name="agent-" + CANARY)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["sense", "--dir", str(self.root)])
        self.assertNotIn(CANARY, out.getvalue())
        files = [p for p in self.home.rglob("*") if p.is_file()]
        self.assertEqual([p.name for p in files], ["sessions.jsonl"])
        for p in files:
            for line in p.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                self.assertIn(CANARY, row["project"])
                self.assertIn(CANARY, row["session"])
                rest = {k: v for k, v in row.items() if k not in ("project", "session")}
                self.assertNotIn(CANARY.lower(), json.dumps(rest).lower())


class Weights(unittest.TestCase):
    def test_use_weights_switches_and_restores(self):
        before = dict(breakdown.PRICES)
        breakdown.use_weights("api-v1")
        self.assertEqual(breakdown.PRICES, before)
        with self.assertRaises(ValueError):
            breakdown.use_weights("../secret")
        self.assertEqual(breakdown.PRICES, before)


if __name__ == "__main__":
    unittest.main()
