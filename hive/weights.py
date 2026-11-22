"""Weights files: USD per million tokens by model and billing kind (plan section 3, item 3)."""
import json
import re
from pathlib import Path

WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"
DEFAULT = "api-v1"
KINDS = ("input", "w5m", "w1h", "read", "output")
ID = re.compile(r"^[a-z0-9-]{1,40}$")


def load(weights_id=DEFAULT):
    """The weights file as a dict; raises ValueError when it is missing or malformed."""
    if not ID.match(weights_id or ""):
        raise ValueError("weights id must match ^[a-z0-9-]{1,40}$")
    path = WEIGHTS_DIR / (weights_id + ".json")
    if not path.is_file():
        raise ValueError("no weights file " + path.name)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("id") != weights_id or not isinstance(data.get("models"), dict):
        raise ValueError("weights file {} has no matching id or no models".format(path.name))
    for model, row in data["models"].items():
        if set(row) != set(KINDS) or not all(isinstance(row[k], (int, float)) for k in KINDS):
            raise ValueError("weights file {}: model {} needs exactly {}".format(path.name, model, KINDS))
    return data


def prices(weights_id=DEFAULT):
    """{model id: {kind: USD per million tokens}}, a fresh copy."""
    return {m: dict(r) for m, r in load(weights_id)["models"].items()}
