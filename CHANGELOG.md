# Changelog

## 0.26.1 (2026-11-22)

- `python -m hive report` has a table of thinking by model: thinking tokens written as output, the cost of re-reading them in later calls, and the total of both with its share of all cost. On the author's machine the total is 30.8% of the weighted cost; the output alone is 18.4%.
- New table: the effort levels found in the logs, with calls and thinking tokens per call by model. Logs without an effort field still work.
- Both tables give the most a lower effort level could remove, not a saving.
- Retrieval server: unchanged, still 0.25.2. The hive package stays 0.1.0.

## 0.26.0 (2026-11-22)

- New: `python -m hive report` shows where the tokens of your Claude Code sessions went, by source, model and token kind. It reads Claude Code's session logs and writes nothing.
- New: `python -m hive sense` keeps one row of numbers per session, `baseline` shows how steady your days are, and `replay bash-output-limit` estimates what cutting long command output would have saved. The estimate comes from your logs and no model call backs it.
- The package also holds the code of a loop that tests one Claude Code setting on random days. It is being tested on the author's machine and is not released or documented yet.
- Retrieval server: unchanged, still 0.25.2.

## 0.25.2 (2026-10-26)

First public release.

- Retrieval and selection: V0.25.0, unchanged.
- Output: compact format. On the 89-question evaluation suite it delivers 24,546 tokens where the original V0.25.0 format delivered 55,710.
- Renamed from "Claude Token Optimizer" to "Token Optimization System". Name only: on the 89 questions, all 267 compact, debug and search outputs are identical to the previous build apart from the name in the first line.
- Windows install, index and Claude Desktop connection scripts.
