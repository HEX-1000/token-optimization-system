"""Change one key of a Claude Code settings file without touching the bytes of anything else.

The file is edited as text: the value of the key is replaced, or a member is inserted after the last
member of its object (copying the indentation and separator the file already uses), or the member is
removed together with the separator that came before it. Inserting and then removing a key therefore
gives back the original bytes. A key path has one or two names: ["autoCompactWindow"] or
["env", "BASH_MAX_OUTPUT_LENGTH"]. A two-name path whose parent object is missing creates it.

The file must be strict JSON with an object at the top; anything else is refused.
"""
import json
from json.decoder import scanstring

_DECODER = json.JSONDecoder()
WS = " \t\n\r"


class SettingsError(ValueError):
    pass


def _skip(s, i):
    while i < len(s) and s[i] in WS:
        i += 1
    return i


def members(s, start):
    """Members of the object whose '{' is at `start`: [(key, key_start, key_end, value_start, value_end)], and the
    position of its closing '}'."""
    if s[start] != "{":
        raise SettingsError("not an object")
    out = []
    i = _skip(s, start + 1)
    if i < len(s) and s[i] == "}":
        return out, i
    while True:
        if i >= len(s) or s[i] != '"':
            raise SettingsError("expected a key at character {}".format(i))
        key, kend = scanstring(s, i + 1)
        j = _skip(s, kend)
        if j >= len(s) or s[j] != ":":
            raise SettingsError("expected ':' at character {}".format(j))
        vs = _skip(s, j + 1)
        try:
            _value, ve = _DECODER.raw_decode(s, vs)
        except ValueError as exc:
            raise SettingsError(str(exc)) from None
        out.append((key, i, kend, vs, ve))
        k = _skip(s, ve)
        if k < len(s) and s[k] == ",":
            i = _skip(s, k + 1)
            continue
        if k < len(s) and s[k] == "}":
            return out, k
        raise SettingsError("expected ',' or '}}' at character {}".format(k))


def _top(s):
    i = _skip(s, 0)
    if i >= len(s) or s[i] != "{":
        raise SettingsError("the settings file does not hold a JSON object")
    json.loads(s)       # strict check of the whole file
    return i


def _locate(s, path):
    """(object start, members, closing position, member or None, parent missing) for the last name of path."""
    start = _top(s)
    mem, close = members(s, start)
    if len(path) == 1:
        hit = next((m for m in mem if m[0] == path[0]), None)
        return start, mem, close, hit, False
    parent = next((m for m in mem if m[0] == path[0]), None)
    if parent is None:
        return start, mem, close, None, True
    if s[parent[3]] != "{":
        raise SettingsError("{} is not an object".format(path[0]))
    pmem, pclose = members(s, parent[3])
    hit = next((m for m in pmem if m[0] == path[1]), None)
    return parent[3], pmem, pclose, hit, False


def get_raw(s, path):
    """The value text of the key, or None when the key is absent."""
    _start, _mem, _close, hit, _missing = _locate(s, path)
    return None if hit is None else s[hit[3]:hit[4]]


def _insert(s, start, mem, close, key, value_text):
    if mem:
        first = mem[0]
        sep_ws = s[start + 1:first[1]]          # whitespace before the first key, as the file has it
        kv = s[first[2]:first[3]]               # the ': ' between key and value, as the file has it
        last = mem[-1]
        piece = "," + sep_ws + json.dumps(key) + kv + value_text
        return s[:last[4]] + piece + s[last[4]:]
    piece = json.dumps(key) + ": " + value_text
    return s[:start + 1] + piece + s[start + 1:]


def set_raw(s, path, value_text):
    """Set the key to a JSON value given as text. Returns (new text, parent created)."""
    json.loads(value_text)
    start, mem, close, hit, parent_missing = _locate(s, path)
    if hit is not None:
        return s[:hit[3]] + value_text + s[hit[4]:], False
    if parent_missing:
        return _insert(s, start, mem, close, path[0], "{" + json.dumps(path[1]) + ": " + value_text + "}"), True
    return _insert(s, start, mem, close, path[-1], value_text), False


def _remove_member(s, mem, j):
    if j > 0:
        return s[:mem[j - 1][4]] + s[mem[j][4]:]
    if len(mem) > 1:
        return s[:mem[0][1]] + s[mem[1][1]:]
    return s[:mem[0][1]] + s[mem[0][4]:]


def remove(s, path, drop_empty_parent=False):
    """Remove the key. With drop_empty_parent, a parent object left empty is removed too."""
    start, mem, close, hit, parent_missing = _locate(s, path)
    if hit is None:
        return s
    j = mem.index(hit)
    s = _remove_member(s, mem, j)
    if len(path) == 2 and drop_empty_parent and len(mem) == 1:
        tstart = _top(s)
        tmem, _ = members(s, tstart)
        pj = next(i for i, m in enumerate(tmem) if m[0] == path[0])
        if not json.loads(s[tmem[pj][3]:tmem[pj][4]]):
            s = _remove_member(s, tmem, pj)
    return s


def value_text(value):
    return json.dumps(value)


def read(path):
    """(text, existed). A missing file reads as '{}' plus a newline."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read(), True
    except FileNotFoundError:
        return "{}\n", False


def write(path, text):
    from .store import write_bytes_atomic
    write_bytes_atomic(path, text.encode("utf-8"))
