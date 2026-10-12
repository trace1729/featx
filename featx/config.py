"""Locations and settings.

The data root defaults to /nfs/share/gkc/featx and can be redirected with FEATX_ROOT
(tests use a tmp dir). Settings = package defaults deep-merged with $FEATX_ROOT/featx.yaml.
"""

from __future__ import annotations

import copy
import functools
import os
from pathlib import Path

import yaml

PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent
DEFAULTS_DIR = PKG_DIR / "defaults"
BIN_DIR = REPO_DIR / "bin"
CONFIG_DIR = REPO_DIR / "config"

DEFAULT_ROOT = "/nfs/share/gkc/featx"


def deep_merge(base, over):
    """Merge `over` into a copy of `base`: dicts recurse, a None value deletes the key,
    everything else (lists included) replaces."""
    if not isinstance(base, dict) or not isinstance(over, dict):
        return copy.deepcopy(over)
    out = copy.deepcopy(base)
    for key, value in over.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def root() -> Path:
    return Path(os.environ.get("FEATX_ROOT", DEFAULT_ROOT))


def tree_root() -> Path:
    return root() / "tree"


def tasks_dir() -> Path:
    return root() / "tasks"


def events_path() -> Path:
    return root() / "events" / "inbox.jsonl"


def history_path() -> Path:
    return root() / "history.jsonl"


def bincache_dir() -> Path:
    return root() / "bincache"


def compare_dir() -> Path:
    return root() / "compare"


def daemon_json() -> Path:
    return root() / "daemon.json"


def lock_path() -> Path:
    return root() / ".tree.lock"


def token_path() -> Path:
    return Path(os.environ.get("FEATX_TOKEN_FILE", Path.home() / ".config" / "featx" / "token"))


@functools.lru_cache(maxsize=8)
def _settings_for(root_str: str) -> dict:
    base = yaml.safe_load((DEFAULTS_DIR / "featx.yaml").read_text()) or {}
    user = Path(root_str) / "featx.yaml"
    if user.is_file():
        base = deep_merge(base, yaml.safe_load(user.read_text()) or {})
    env_wt = os.environ.get("FEATX_WT_ROOT")
    if env_wt:
        base["wt_root"] = env_wt
    return base


def settings() -> dict:
    return _settings_for(str(root()))


def reload_settings() -> None:
    _settings_for.cache_clear()


def wt_root() -> Path:
    return Path(settings()["wt_root"])


def util_dir() -> Path:
    return Path(settings()["util_dir"])


def ensure_layout() -> None:
    for d in (tree_root(), tasks_dir(), events_path().parent, bincache_dir(), compare_dir()):
        d.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------- editing
RESTART_KEYS = ("daemon",)   # read once when featxd starts


def defaults() -> dict:
    return yaml.safe_load((DEFAULTS_DIR / "featx.yaml").read_text()) or {}


def user_layer_path() -> Path:
    return root() / "featx.yaml"


def validate_user_layer(data, schema=None, prefix: str = "") -> list[str]:
    """The user layer may only override keys that exist in the defaults, with the same type
    (int/float interchangeable; None deletes a key, see deep_merge)."""
    schema = defaults() if schema is None else schema
    if not isinstance(data, dict):
        return [f"{prefix or 'top level'}: expected a mapping, got {type(data).__name__}"]
    errors = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if key not in schema:
            errors.append(f"{path}: unknown key (known: {', '.join(sorted(schema))})")
            continue
        want = schema[key]
        if value is None:
            continue
        if isinstance(want, dict):
            errors += validate_user_layer(value, want, path + ".")
        elif isinstance(want, bool) or isinstance(value, bool):
            if not (isinstance(want, bool) and isinstance(value, bool)):
                errors.append(f"{path}: expected {type(want).__name__}, got {type(value).__name__}")
        elif isinstance(want, (int, float)):
            if not isinstance(value, (int, float)):
                errors.append(f"{path}: expected a number, got {type(value).__name__}")
        elif not isinstance(value, type(want)):
            errors.append(f"{path}: expected {type(want).__name__}, got {type(value).__name__}")
    return errors


def config_view() -> dict:
    path = user_layer_path()
    text = path.read_text() if path.is_file() else ""
    return {"path": str(path), "user_text": text, "user": yaml.safe_load(text) or {} if text else {},
            "defaults_text": (DEFAULTS_DIR / "featx.yaml").read_text(), "effective": settings(),
            "restart_keys": list(RESTART_KEYS)}


def write_user_layer(text: str) -> dict:
    """Validate and replace $FEATX_ROOT/featx.yaml; returns the keys that need a daemon restart."""
    from . import store
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML: {error}") from error
    errors = validate_user_layer(data or {})
    if errors:
        raise ValueError("; ".join(errors))
    before = settings()
    store.atomic_write_text(user_layer_path(), text if text.endswith("\n") or not text else text + "\n")
    reload_settings()
    after = settings()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return {"path": str(user_layer_path()), "changed": changed,
            "restart_needed": [k for k in changed if k in RESTART_KEYS]}
