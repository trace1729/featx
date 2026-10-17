"""Locations and settings.

The data root is $FEATX_ROOT, else the path in ~/.config/featx/root (written by `featx init`),
else ~/.local/share/featx. Settings, later wins:
  package defaults (featx/defaults/featx.yaml)
  -> the site profile named by `site:` (featx/sites/<name>.yaml or a YAML path): shared,
     cluster-wide values (workloads, checkpoint roots, tools on shared storage, hosts)
  -> $FEATX_ROOT/featx.yaml (the user layer: personal paths, ports, caps)
  -> FEATX_WT_ROOT.
${root}, ${home}, ${USER} and ${share} in string values expand to the data root, the user's home, login name and
the `share:` setting (the shared storage mount a site profile's paths sit under).
"""

from __future__ import annotations

import copy
import functools
import getpass
import os
import re
from pathlib import Path

import yaml

PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent          # ${featx.home}: holds featx/ (a checkout or site-packages)
DEFAULTS_DIR = PKG_DIR / "defaults"
SITES_DIR = PKG_DIR / "sites"
BIN_DIR = PKG_DIR / "bin"          # featx-exec, runwatch.sh, add-worktree.sh
DOCS_DIR = PKG_DIR / "docs"

# Keys whose values are free-form mappings (any names below them).
FREEFORM = ("workloads", "recipe_defaults", "baselines")


class ConfigError(RuntimeError):
    pass


def config_home() -> Path:
    """Per-user featx config dir: the token and the root pointer."""
    return Path(os.environ.get("FEATX_CONFIG_HOME") or Path.home() / ".config" / "featx")


def default_root() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "featx"


def root_pointer() -> Path:
    return config_home() / "root"


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


@functools.lru_cache(maxsize=4)
def _pointed_root(pointer: str) -> str | None:
    try:
        text = Path(pointer).read_text().strip()
    except OSError:
        return None
    return text or None


def root() -> Path:
    env = os.environ.get("FEATX_ROOT")
    if env:
        return Path(env)
    pointed = _pointed_root(str(root_pointer()))
    return Path(pointed) if pointed else default_root()


def set_root_pointer(path: Path) -> Path:
    ptr = root_pointer()
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text(str(path) + "\n")
    _pointed_root.cache_clear()
    reload_settings()
    return ptr


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
    return Path(os.environ.get("FEATX_TOKEN_FILE") or config_home() / "token")


# ------------------------------------------------------------- site profiles
def sites() -> list[str]:
    return sorted(p.stem for p in SITES_DIR.glob("*.yaml"))


def site_path(name: str) -> Path:
    """A site name (featx/sites/<name>.yaml) or a path to a YAML file."""
    if "/" in name or name.endswith((".yaml", ".yml")):
        return Path(name).expanduser()
    return SITES_DIR / f"{name}.yaml"


def site_layer(name: str | None) -> dict:
    if not name:
        return {}
    path = site_path(name)
    if not path.is_file():
        raise ConfigError(f"site profile {name!r} not found ({path}); known: {', '.join(sites()) or 'none'}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"site profile {path}: expected a mapping")
    data.pop("site", None)
    data.pop("detect", None)
    return data


def detect_site() -> str | None:
    """The first site profile whose `detect:` paths all exist on this host (what `featx init`
    picks without --site)."""
    for name in sites():
        try:
            data = yaml.safe_load(site_path(name).read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        detect = data.get("detect") if isinstance(data, dict) else None
        values = {"share": str(data.get("share") or "").rstrip("/"), "USER": os.environ.get("USER") or getpass.getuser()}
        if detect and all(Path(expand(p, values, data.get("share_fallbacks") or [])).exists()
                          for p in detect):
            return name
    return None


_EXPAND_RE = re.compile(r"\$\{(root|home|share|USER)\}")


def expand(obj, values: dict, share_fallbacks=()):
    """${root} / ${home} / ${USER} / ${share} in every string of obj (other ${...}, and ones whose value
    is unset, pass through). Shared paths use the first existing primary/fallback path;
    if none exists, retain the primary path so diagnostics name the requested input."""
    if isinstance(obj, str):
        expanded = _EXPAND_RE.sub(lambda m: values.get(m.group(1)) or m.group(0), obj)
        if "${share}/" in obj and share_fallbacks:
            # Recipe paths can contain deferred ${point.*} variables. Check their
            # containing directory until those variables can be resolved at run time.
            def exists(path):
                if "${" in path:
                    path = path.split("${", 1)[0].rsplit("/", 1)[0] or "/"
                return Path(path).exists()

            for share in [values.get("share"), *share_fallbacks]:
                if not share:
                    continue
                candidate = _EXPAND_RE.sub(
                    lambda m: str(share).rstrip("/") if m.group(1) == "share"
                    else values.get(m.group(1)) or m.group(0), obj)
                if exists(candidate):
                    return candidate
        return expanded
    if isinstance(obj, dict):
        return {k: expand(v, values, share_fallbacks) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand(v, values, share_fallbacks) for v in obj]
    return obj


def _read_layer(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return data


def without_trace(kind: str, data: dict) -> dict:
    """Remove TraceRTL-only settings from an effective config, preserving stored layers."""
    data = copy.deepcopy(data)
    if kind == "run":
        trace_root = data.get("trace_root")
        perf_trigger = data.get("perf_trigger")
        if trace_root and isinstance(perf_trigger, dict) and perf_trigger.get("gcpt_path") == trace_root:
            perf_trigger.pop("gcpt_path", None)
        for key in ("trace_root", "trace_file"):
            data.pop(key, None)
        if isinstance(data.get("commands"), dict):
            data["commands"].pop("trace", None)
    if kind in ("build", "run") and isinstance(data.get("env"), dict):
        data["env"] = {k: v for k, v in data["env"].items() if not k.startswith("TRACERTL_")}
    return data


@functools.lru_cache(maxsize=8)
def _settings_for(root_str: str) -> dict:
    base = defaults()
    user = _read_layer(Path(root_str) / "featx.yaml")
    site = os.environ.get("FEATX_SITE") or user.get("site")
    if site:
        base = deep_merge(base, site_layer(site))
    base = deep_merge(base, user)
    base["site"] = site or None
    env_wt = os.environ.get("FEATX_WT_ROOT")
    if env_wt:
        base["wt_root"] = env_wt
    share = str(base.get("share") or "").rstrip("/")
    base = expand(base, {"root": root_str, "home": str(Path.home()), "share": share, "USER": os.environ.get("USER") or getpass.getuser()},
                  base.get("share_fallbacks") or [])
    if not base.get("trace_enabled", False):
        git = base.get("git") or {}
        git["submodule_worktrees"] = [s for s in git.get("submodule_worktrees", []) if s != "TraceRTL"]
        recipe_defaults = base.setdefault("recipe_defaults", {})
        for kind in ("build", "run"):
            recipe_defaults.setdefault(kind, {})["mode"] = "exec"
            recipe_defaults[kind] = without_trace(kind, recipe_defaults[kind])
        for workload in (base.get("workloads") or {}).values():
            if not isinstance(workload, dict):
                continue
            workload.pop("manifest", None)
            if isinstance(workload.get("run"), dict):
                workload["run"] = without_trace("run", workload["run"])
    paths = base.get("paths")
    if isinstance(paths, dict) and paths.get("nemu_diff") is None:
        nemu = repo_nemu((base.get("git") or {}).get("repo"))
        if nemu:
            paths["nemu_diff"] = nemu
    return base


NEMU_SO = "ready-to-run/riscv64-nemu-interpreter-so"


def repo_nemu(repo: str | None) -> str | None:
    """The XiangShan checkout's own NEMU reference .so (paths.nemu_diff when that is unset)."""
    if not repo:
        return None
    so = Path(repo) / NEMU_SO
    return str(so) if so.is_file() else None


def settings() -> dict:
    return _settings_for(str(root()))


def reload_settings() -> None:
    _settings_for.cache_clear()


ANALYSIS_DIR = PKG_DIR / "post" / "scripts" / "analysis"


def analysis_dir() -> Path:
    """The spec_trace_compare analysis scripts (${tools}): featx.yaml analysis_dir, else the
    copy shipped with featx."""
    return Path(settings().get("analysis_dir") or ANALYSIS_DIR)


def wt_root() -> Path:
    return Path(settings()["wt_root"])


def paths() -> dict:
    """Tool and checkout paths (featx.yaml `paths`); unset ones are None."""
    return dict(settings().get("paths") or {})


def require(dotted: str) -> str:
    """A settings value that must be set (e.g. git.repo), with a hint when it is not."""
    node = settings()
    for part in dotted.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if node in (None, ""):
        raise ConfigError(f"{dotted} is not set; set it in {user_layer_path()} "
                          f"(featx config set {dotted}=...) or pick a site profile (featx init --site ...)")
    return node


def ensure_layout() -> None:
    for d in (tree_root(), tasks_dir(), events_path().parent, bincache_dir(), compare_dir()):
        d.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------- editing
RESTART_KEYS = ("daemon",)   # read once when featxd starts


def defaults() -> dict:
    return yaml.safe_load((DEFAULTS_DIR / "featx.yaml").read_text()) or {}


def user_layer_path() -> Path:
    return root() / "featx.yaml"


def schema(site: str | None = None) -> dict:
    """What a user layer may set: the defaults plus the keys the site profile adds."""
    base = defaults()
    try:
        return deep_merge(base, site_layer(site)) if site else base
    except ConfigError:
        return base


def validate_user_layer(data, schema_=None, prefix: str = "") -> list[str]:
    """The user layer may only override keys that exist in the defaults (or the site profile),
    with the same type (int/float interchangeable; None deletes a key, see deep_merge). A key
    whose default is null takes any value; the FREEFORM sections take any names."""
    if schema_ is None:
        if isinstance(data, dict) and data.get("site"):
            try:
                site_layer(data["site"])
            except ConfigError as error:
                return [f"site: {error}"]
        schema_ = schema(data.get("site") if isinstance(data, dict) else None)
    if not isinstance(data, dict):
        return [f"{prefix or 'top level'}: expected a mapping, got {type(data).__name__}"]
    errors = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if key not in schema_:
            errors.append(f"{path}: unknown key (known: {', '.join(sorted(schema_))})")
            continue
        want = schema_[key]
        if value is None or want is None:
            continue
        if not prefix and key in FREEFORM:
            if not isinstance(value, dict):
                errors.append(f"{path}: expected a mapping, got {type(value).__name__}")
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
    site = settings().get("site")
    site_file = site_path(site) if site else None
    return {"path": str(path), "user_text": text, "user": yaml.safe_load(text) or {} if text else {},
            "defaults_text": (DEFAULTS_DIR / "featx.yaml").read_text(), "effective": settings(),
            "site": site, "site_path": str(site_file) if site_file else None,
            "site_text": site_file.read_text() if site_file and site_file.is_file() else "",
            "sites": sites(), "root": str(root()), "restart_keys": list(RESTART_KEYS)}


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


def set_user_key(key: str, value) -> dict:
    """Set one dotted key (e.g. placement.max_total_cores) in the user layer; None removes it.
    The layer is re-dumped, so YAML comments in it are not kept."""
    path = user_layer_path()
    data = (yaml.safe_load(path.read_text()) if path.is_file() else None) or {}
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ValueError(f"{key}: {part} is not a mapping in {path}")
    if value is None:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value
    node = schema(data.get("site"))
    for i, part in enumerate(parts):
        if not isinstance(node, dict) or part not in node:
            if i and parts[0] in FREEFORM:
                break
            raise ValueError(f"{key}: unknown key")
        node = node[part]
    text = yaml.safe_dump(data, sort_keys=False) if data else ""
    return write_user_layer(text)
