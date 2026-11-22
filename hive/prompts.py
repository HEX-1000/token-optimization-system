"""Typed prompts of a session file: the rules of the private tool week_prompts.py, counts only.

A prompt is a message of the user (type "user") that is not a tool result, not marked meta, not
written inside a subagent (isSidechain), once the blocks the programs add (system reminders, IDE
context) are cut out. Slash commands and their output, shell lines typed with !, "[Request
interrupted" messages and the summary Claude Code writes when a session is continued are not
prompts; they are counted by reason. No text is kept: only counts and the time of each prompt.
"""
import json
import re
from collections import Counter

INJECTED_TAGS = ("system-reminder", "ide_opened_file", "ide_selection",
                 "local-command-caveat", "user-prompt-submit-hook")
INJECTED = re.compile(
    r"<(%s)\b[^>]*>.*?</\1>" % "|".join(re.escape(t) for t in INJECTED_TAGS), re.S)
SKIP_PREFIXES = (
    ("slash command", ("<command-name>", "<command-message>", "<command-args>")),
    ("command output", ("<local-command-stdout>", "<local-command-stderr>")),
    ("shell line", ("<bash-input>", "<bash-stdout>", "<bash-stderr>")),
    ("interrupt", ("[Request interrupted",)),
    ("continuation summary", ("This session is being continued from a previous conversation",)),
)
REASONS = ("empty",) + tuple(r for r, _ in SKIP_PREFIXES)


def message_text(content):
    """The text of a user message: a string, or the text blocks of a list joined by a newline."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"
                         and isinstance(b.get("text"), str))
    return ""


def classify(text):
    """None for a prompt, or the reason the message is not one."""
    clean = INJECTED.sub("", text).strip()
    if not clean:
        return "empty"
    for reason, prefixes in SKIP_PREFIXES:
        if clean.startswith(prefixes):
            return reason
    return None


def count_prompts(path):
    """(number of prompts, Counter of reasons for the messages left out, prompt timestamps) of one file.

    The record filter and the order of checks are those of week_prompts.read_prompts."""
    prompts, skipped, times, seen = 0, Counter(), [], set()
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "user":
            records.append(obj)
    for obj in records:
        if obj.get("isMeta") or obj.get("isSidechain") or obj.get("toolUseResult") is not None:
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            continue
        uid = obj.get("uuid")
        if uid and uid in seen:
            continue
        seen.add(uid)
        reason = classify(message_text(content))
        if reason:
            skipped[reason] += 1
            continue
        prompts += 1
        ts = obj.get("timestamp")
        times.append(ts if isinstance(ts, str) else None)
    return prompts, skipped, times
