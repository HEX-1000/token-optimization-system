"""Local data of a node, outside any repository.

Default folder: ~/.token-optimization-system/hive (on Windows under %USERPROFILE%). The environment
variable HIVE_HOME points it elsewhere (tests use a temporary folder).
"""
import json
import os
import tempfile
from pathlib import Path

ENV = "HIVE_HOME"


def data_dir():
    d = Path(os.environ[ENV]) if os.environ.get(ENV) else Path.home() / ".token-optimization-system" / "hive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_bytes_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, obj):
    write_bytes_atomic(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def write_jsonl(path, rows):
    text = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)
    write_bytes_atomic(path, text.encode("utf-8"))


def append_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
