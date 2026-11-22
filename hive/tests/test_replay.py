"""Tests of the replay screen bash_output_limit v2, with the numbers worked out by hand."""
import math
import tempfile
import unittest
from pathlib import Path

from hive import breakdown, replay
from hive.tests.logs import Log, tool_use, use

READ = 0.20e-6          # Opus 5.5 cache read, USD per token (api-v1)


def build(mention, prompt="go"):
    """Six calls. Two Bash results of 9,000 characters enter as single items of exactly 3,000 tokens:
    t1 at call index 1 (carried in calls 2 to 5), t2 at call index 2 (carried in calls 3 to 5).
    The removed part of t1 holds NEEDEDWORD_alpha123, of t2 UNUSEDWORD_beta4567; call 3 names the
    first one when `mention` is true. `prompt` is the first record's text."""
    log = Log()
    u = log.user(prompt)
    a = log.call("m1", [tool_use("t1", "Bash", {"command": "make build"})], use(cw=10000, out=10), u)
    r = log.result("t1", "a" * 2000 + " NEEDEDWORD_alpha123 " + "b" * (7000 - 21), a[-1])
    a = log.call("m2", [tool_use("t2", "Bash", {"command": "cat log"})], use(cw=3010, cr=10000, out=10), r)
    r = log.result("t2", "c" * 2000 + " UNUSEDWORD_beta4567 " + "d" * (7000 - 21), a[-1])
    text = "I see NEEDEDWORD_alpha123" if mention else "I see it"
    a = log.call("m3", [{"type": "text", "text": text}], use(cw=3010, cr=13010, out=5), r)
    prev, ctx = a[-1], 16020
    for k in range(3):
        u = log.user("next", prev)
        a = log.call("n%d" % k, [{"type": "text", "text": "ok"}], use(cw=100, cr=ctx, out=5), u)
        prev, ctx = a[-1], ctx + 105
    return log


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def screen(self, mention, prompt="go"):
        build(mention, prompt).write(self.root)
        sessions = breakdown.load_sessions([self.root])
        items = {it.tid: it for it in sessions[0].fd.items if it.tid}
        self.assertEqual((items["t1"].size, items["t2"].size), (3000.0, 3000.0))
        return replay.screen(sessions, [4000, 8000, 15000]), sessions

    def test_a_needed_result_costs_one_extra_call(self):
        r, sessions = self.screen(True)
        lim = r["limits"][4000]
        self.assertEqual((lim["cut"], lim["needed"]), (2, 1))
        saving = 3000 * 7000 / 9000 * 3 * READ          # t2 only
        extra = 13010 * READ                           # t1: the context of the call that first sees it
        self.assertTrue(math.isclose(lim["saving"], saving, rel_tol=1e-9))
        self.assertTrue(math.isclose(lim["extra"], extra, rel_tol=1e-9))
        self.assertTrue(math.isclose(lim["net"], saving - extra, rel_tol=1e-9))
        self.assertEqual(lim["needed_rate"], 0.5)
        self.assertEqual((lim["needed_upper"], lim["needed_upper_rate"]), (1, 0.5))
        self.assertFalse(lim["passes"])
        self.assertIsNone(r["proposed"])
        self.assertEqual(r["replay_code"], "2")

    def test_a_word_of_an_earlier_record_does_not_make_a_result_needed(self):
        # The prompt names the word before t1 runs: version 2 counts no need, version 1 (the upper bound) does.
        r, sessions = self.screen(True, prompt="go, and look for NEEDEDWORD_alpha123")
        total = sum(v[3] for v in sessions[0].rows.values())
        saving = 3000 * 7000 / 9000 * (4 + 3) * READ   # both results save their carried reads
        lim = r["limits"][4000]
        self.assertEqual((lim["cut"], lim["needed"], lim["needed_upper"]), (2, 0, 1))
        self.assertEqual((lim["needed_rate"], lim["needed_upper_rate"]), (0.0, 0.5))
        self.assertEqual(lim["extra"], 0.0)
        self.assertTrue(math.isclose(lim["net"], saving, rel_tol=1e-9))
        self.assertTrue(math.isclose(lim["net_saving_share"], saving / total, rel_tol=1e-9))
        self.assertTrue(lim["passes"])
        self.assertEqual(r["proposed"], 4000)

    def test_needed_rule_on_its_own(self):
        text = "k" * replay.PREVIEW_CHARS + " REMOVED_word_1 OTHER_word_22 "
        later = ["now REMOVED_word_1"]
        history = "earlier OTHER_word_22\nthe result " + text
        end = len("earlier OTHER_word_22\n")
        self.assertEqual(replay.needed(text, "", later, history, end), (True, True))      # in no earlier record
        self.assertEqual(replay.needed(text, "", later, "x REMOVED_word_1\n" + history, end + 17),
                         (False, True))                                                   # in an earlier record
        self.assertEqual(replay.needed(text, "", ["now OTHER_word_22"], history, end), (False, True))
        self.assertEqual(replay.needed(text, "", ["nothing here"], history, end), (False, False))
        self.assertEqual(replay.needed(text, "cat REMOVED_word_1", later, history, end), (False, False))
        # the upper bound is version 1's rule: any removed word, not in the kept part or the command, named later
        for later_ in (later, ["now OTHER_word_22"], ["nothing"]):
            v1 = any(w in rec for rec in later_ for w in {"REMOVED_word_1", "OTHER_word_22"})
            self.assertEqual(replay.needed(text, "", later_, history, end)[1], v1)

    def test_every_string_value_of_an_earlier_record_counts(self):
        log = build(True)
        log.recs[0]["extra"] = {"list": [1, "NEEDEDWORD_alpha123"]}       # nested, outside the message
        log.write(self.root)
        sessions = breakdown.load_sessions([self.root])
        texts, history = replay.raw_pass(sessions[0].path, {"t1"})
        text, later, end = texts["t1"]
        self.assertGreaterEqual(history.find("NEEDEDWORD_alpha123"), 0)
        self.assertLess(history.find("NEEDEDWORD_alpha123"), end)
        self.assertGreater(history.find("NEEDEDWORD_alpha123", end), end)      # the result's own record
        self.assertEqual(replay.screen(sessions, [4000])["limits"][4000]["needed"], 0)

    def test_results_not_needed_save_their_carried_reads(self):
        r, sessions = self.screen(False)
        total = sum(v[3] for v in sessions[0].rows.values())
        saving = 3000 * 7000 / 9000 * (4 + 3) * READ
        for limit in (4000, 8000):
            lim = r["limits"][limit]
            self.assertEqual((lim["cut"], lim["needed"]), (2, 0))
            self.assertTrue(math.isclose(lim["net"], saving, rel_tol=1e-9))
            self.assertTrue(math.isclose(lim["net_saving_share"], saving / total, rel_tol=1e-9))
            self.assertGreater(lim["net_saving_share"], replay.PASS_SAVING)
            self.assertTrue(lim["passes"])
        self.assertEqual(r["limits"][15000]["cut"], 0)
        self.assertFalse(r["limits"][15000]["passes"])
        self.assertEqual(r["proposed"], 4000)

    def test_failed_results_are_not_cut_and_powershell_is(self):
        log = build(False)
        for rec in log.recs:
            msg = rec.get("message", {})
            for b in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                if b.get("type") == "tool_result" and b.get("tool_use_id") == "t1":
                    b["is_error"] = True
                if b.get("type") == "tool_use" and b.get("id") == "t2":
                    b["name"] = "PowerShell"
        log.write(self.root)
        r = replay.screen(breakdown.load_sessions([self.root]), [4000])
        self.assertEqual(r["limits"][4000]["cut"], 1)                  # t2, the PowerShell result

    def test_render_has_numbers_and_no_text(self):
        r, _ = self.screen(True)
        text = replay.render(r)
        self.assertNotIn("NEEDEDWORD", text)
        self.assertNotIn("make build", text)
        self.assertIn("v2", text)
        self.assertIn("upper %", text)


if __name__ == "__main__":
    unittest.main()
