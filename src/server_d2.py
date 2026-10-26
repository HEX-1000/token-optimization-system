"""
Token Optimization System 0.25.2: V0.25.0 retrieval with D2 compact output

Presentation-only change on top of V0.25.0. Retrieval is not reimplemented
here: every optimizer_context call runs the unmodified V0.25
server.optimizer_context(), which performs RETRIEVE → CLASSIFY → RANK →
SELECT with its own pool-size rule and max_chunks clamp, computes its metrics
and writes its usage event. This module records the SELECT result of that same
call and renders it in a shorter form.

    optimizer_context(query, max_chunks=2, debug=False)

debug=False (normal use): one header line (version and number of selected
sections, or why nothing was selected); each selected chunk text, unchanged,
under a short file label that adds the section name only when the chunk text
does not already start with it; and the names of other sections that matched
but were not included.

debug=True: the V0.25 output of the same call, byte for byte, except that the
first line reports version 0.25.2.

server.py, indexer.py and db.py are not modified. The database path, the
usage log and the other three tools are V0.25's.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

SERVER_VERSION = "0.25.2"

import server as v025  # V0.25.0, unmodified: the source of truth for retrieval

if Path(v025.__file__).resolve().parent != Path(__file__).resolve().parent:
    raise ImportError(
        f"server_d2 must import the V0.25 server.py next to it, got {v025.__file__}"
    )

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Token Optimization System")


# ============================================================================
# CAPTURING THE V0.25 SELECTION
# ============================================================================

_V025_FIRST_LINE = f"Token Optimization System v{v025.SERVER_VERSION}"
_D2_FIRST_LINE = f"Token Optimization System v{SERVER_VERSION}"

# stage_select is looked up in server.py's module globals when
# optimizer_context runs, so a recording wrapper installed for the duration of
# one call sees exactly the selection that call makes. The lock keeps two
# calls from sharing the wrapper.
_capture_lock = threading.Lock()


def run_v025_context(query: str, max_chunks: int = 2) -> tuple[str, dict[str, Any] | None]:
    """
    Run V0.25 optimizer_context() unchanged and return its output together
    with the stage_select() result of the same call.

    The returned selection is stage_select's own result plus
    "candidate_count", the length of the ranked list it was given. It is None
    when RETRIEVE found no candidates (V0.25 returns before SELECT then).
    """
    captured: list[dict[str, Any]] = []
    original_select = v025.stage_select

    def recording_select(ranked_candidates: list[dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_select(ranked_candidates, *args, **kwargs)
        captured.append({**result, "candidate_count": len(ranked_candidates)})
        return result

    with _capture_lock:
        v025.stage_select = recording_select
        try:
            output = v025.optimizer_context(query=query, max_chunks=max_chunks)
        finally:
            v025.stage_select = original_select

    if len(captured) > 1:
        raise RuntimeError("V0.25 optimizer_context called stage_select more than once")
    return output, (captured[0] if captured else None)


def with_d2_version(output: str) -> str:
    """Replace the V0.25 version line (always the first line) with 0.25.2."""
    first, newline, rest = output.partition("\n")
    if first != _V025_FIRST_LINE:
        raise RuntimeError(f"unexpected first line in V0.25 output: {first!r}")
    return _D2_FIRST_LINE + newline + rest


# ============================================================================
# COMPACT RENDERING
# ============================================================================

# Same bound as V0.25's "Skipped:" list, which prints at most 5 entries.
SKIPPED_NAMES_LIMIT = 5

HEADER_PREFIX = f"Token Optimization System {SERVER_VERSION}: "
SKIPPED_PREFIX = "Also matched, not included: "
LABEL_SEPARATOR = " | "

# The heading rule indexer.chunk_markdown() uses to name Markdown sections.
_ATX_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


def header_line(selected_count: int, candidate_count: int) -> str:
    if candidate_count == 0:
        return HEADER_PREFIX + "no indexed section matches the query terms."
    if selected_count == 0:
        if candidate_count == 1:
            return HEADER_PREFIX + "1 candidate section was found, but it did not pass the relevance threshold."
        return (HEADER_PREFIX + f"{candidate_count} candidate sections were found, "
                "but none passed the relevance threshold.")
    return HEADER_PREFIX + f"{selected_count} section{'s' if selected_count != 1 else ''} selected."


def split_path(path: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", path) if part]


def indexed_paths(conn: Any) -> list[str]:
    """Every file path the index knows about (files table and chunks)."""
    rows = conn.execute(
        "SELECT path FROM files UNION SELECT DISTINCT path FROM chunks"
    ).fetchall()
    return sorted({str(row[0]) for row in rows})


def compact_file_label(path: str, all_paths: list[str]) -> str:
    """
    Shortest trailing part of the path that names exactly one indexed file.

    Usually the file name. When two indexed files share the file name, parent
    folders are added until the label is unique ("corpus/notes.md"). If no
    suffix is unique, the full path is used.
    """
    parts = split_path(path)
    others = [split_path(p) for p in all_paths if p != path]
    for k in range(1, len(parts) + 1):
        suffix = parts[-k:]
        if not any(len(o) >= k and o[-k:] == suffix for o in others):
            return "/".join(suffix)
    return path


def section_shown_by_content(section: str, content: str) -> bool:
    """
    True when the chunk text already starts with the section name, so
    "| section" on the label line would repeat it.

    The first line counts as the section name when, after removing a Markdown
    heading marker (the indexer's rule), it equals the section name exactly.
    This covers Markdown and DOCX headings. PDF pages ("page 3"), PPTX slides
    ("slide 2: Title") and XLSX sheets ("sheet: Data") never start with their
    section name, so they keep the full label.
    """
    first_line = content.split("\n", 1)[0]
    match = _ATX_HEADING.match(first_line)
    heading = match.group(2).strip() if match else first_line.strip()
    return heading == section


def chunk_label(index: int, path: str, section: str, content: str, all_paths: list[str]) -> str:
    label = f"[{index}] {compact_file_label(path, all_paths)}"
    if section_shown_by_content(section, content):
        return label
    return label + LABEL_SEPARATOR + section


def render_compact(selection: dict[str, Any] | None, all_paths: list[str]) -> str:
    """
    Compact output:

        Token Optimization System 0.25.2: 2 sections selected.

        [1] networking_v1.0.md
        ## Topic M — Common Operational Difficulties
        <chunk text, unchanged>

        [2] report.pdf | page 3
        <chunk text, unchanged>

        Also matched, not included: <section>; <section>
    """
    if selection is None:
        return header_line(0, 0)

    selected = selection["selected"]
    skipped_names = [entry["section"] for entry in selection["skipped"][:SKIPPED_NAMES_LIMIT]]

    lines = [header_line(len(selected), selection["candidate_count"])]
    for index, candidate in enumerate(selected, start=1):
        lines.append("")
        lines.append(chunk_label(index, str(candidate["path"]), str(candidate["section"]),
                                 str(candidate["content"]), all_paths))
        lines.append(str(candidate["content"]))

    if skipped_names:
        lines.append("")
        lines.append(SKIPPED_PREFIX + "; ".join(skipped_names))

    return "\n".join(lines)


def compact_context(query: str, max_chunks: int = 2) -> tuple[str, str, dict[str, Any] | None]:
    """Return (compact output, V0.25 output, selection) for one call."""
    v025_output, selection = run_v025_context(query, max_chunks)
    if selection is None or not selection["selected"]:
        return render_compact(selection, []), v025_output, selection
    conn = v025.get_connection()
    try:
        all_paths = indexed_paths(conn)
    finally:
        conn.close()
    return render_compact(selection, all_paths), v025_output, selection


# ============================================================================
# MCP TOOLS
# ============================================================================


@mcp.tool()
def optimizer_context(query: str, max_chunks: int = 2, debug: bool = False) -> str:
    """
    Retrieve the highest-value local context for a query.

    Returns the selected sections, each under a short file label, followed by
    the names of other matching sections that were not included.
    max_chunks is an upper bound, not a requirement.

    Leave debug disabled in normal use. debug=True returns the full
    diagnostic output (query shape, ranks, scores, signals, skip reasons,
    absolute paths and metrics) for troubleshooting only; it costs about
    twice as many tokens.
    """
    if debug:
        output, _ = run_v025_context(query, max_chunks)
        return with_d2_version(output)
    compact, _, _ = compact_context(query, max_chunks)
    return compact


@mcp.tool()
def optimizer_search(query: str, limit: int = 5) -> str:
    """Search indexed local content and return the most relevant sections."""
    return with_d2_version(v025.optimizer_search(query=query, limit=limit))


@mcp.tool()
def optimizer_read(path: str, section: str = "") -> str:
    """Read indexed content from an allowlisted local file."""
    return v025.optimizer_read(path=path, section=section)


@mcp.tool()
def optimizer_stats() -> str:
    """Show local optimizer usage and estimated context savings."""
    return with_d2_version(v025.optimizer_stats())


# ============================================================================
# SERVER ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    mcp.run()
