"""Failure classification from log tails (rules in config/classify.yaml)."""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

import yaml

from .. import config

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TAIL_BYTES = 256 * 1024


@functools.lru_cache(maxsize=4)
def _rules(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    for rule in data.get("rules", []):
        if rule.get("regex"):
            rule["_re"] = re.compile(rule["regex"], re.MULTILINE)
        if rule.get("unless_regex"):
            rule["_unless"] = re.compile(rule["unless_regex"], re.MULTILINE)
    return data


def rules_path() -> str:
    user = config.root() / "classify.yaml"
    return str(user if user.is_file() else config.CONFIG_DIR / "classify.yaml")


def tail_text(path: Path, nbytes: int = TAIL_BYTES, skip_perf: bool = True) -> str:
    """Last nbytes of a log, ANSI-stripped, without [PERF] lines (they are ~35k lines of
    counters and would push the diagnostic text out of the window)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > nbytes:
                handle.seek(size - nbytes)
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    data = ANSI_RE.sub("", data).replace("\r", "\n")
    if skip_perf:
        data = "\n".join(l for l in data.split("\n") if not l.startswith("[PERF"))
    return data


def last_lines(text: str, n: int = 80) -> str:
    lines = [l for l in text.split("\n") if l.strip()]
    return "\n".join(lines[-n:])


def classify(kind: str, rc: int | None, text: str) -> dict:
    data = _rules(rules_path())
    rc_val = -1 if rc is None else rc
    for rule in data.get("rules", []):
        kinds = rule.get("kinds")
        if kinds and kind not in kinds:
            continue
        if "rc" in rule and rc_val not in rule["rc"]:
            continue
        if "_unless" in rule and rule["_unless"].search(text):
            continue
        groups: tuple = ()
        if "_re" in rule:
            # Prefer the earliest occurrence: the first error is the cause, later ones are fallout.
            match = rule["_re"].search(text)
            if not match:
                continue
            groups = match.groups() or (match.group(0),)
        signature = rule.get("signature", rule["name"])
        try:
            signature = signature.format(*[g.strip() if g else "" for g in groups], rc=rc_val)
        except (IndexError, KeyError):
            pass
        return {"class": rule["class"], "rule": rule["name"], "signature": signature}
    default = data.get("default", {"class": "design", "signature": "exit code {rc}"})
    return {"class": default["class"], "rule": "default",
            "signature": default.get("signature", "exit code {rc}").format(rc=rc_val)}
