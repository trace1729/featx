"""`featx init`: create (or adopt) a data root and write its user layer, the root pointer
(~/.config/featx/root, so later commands and the MCP server find the root without FEATX_ROOT)
and the API token."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

from . import config, store

BASE_PORT = 8765


def default_port() -> int:
    """8765 + uid % 200: users sharing a daemon host get different ports by default."""
    return BASE_PORT + os.getuid() % 200


def init(root: str | None = None, site: str | None = None, repo: str | None = None,
         daemon_host: str | None = None, port: int | None = None, adopt: bool = False,
         extra: dict | None = None) -> dict:
    """Write the user layer of `root` (default: the current root). A root with a featx.yaml
    already needs adopt=True: the given keys are then merged into it, everything else in it
    stays (an existing setup keeps its port unless one is given).
    Without `site` a layer that has none gets the site profile detected on this host
    (config.detect_site); site="none" skips that."""
    target = Path(os.path.abspath(Path(root).expanduser())) if root else config.root()
    layer_path = target / "featx.yaml"
    existing = (yaml.safe_load(layer_path.read_text()) or {}) if layer_path.is_file() else None
    if existing is not None and not adopt:
        raise config.ConfigError(f"{layer_path} exists; pass --adopt to merge the given keys into it")
    detected = False
    if site == "none":
        site = None
    elif site is None and not (existing or {}).get("site"):
        site = config.detect_site()
        detected = bool(site)
    if site:
        config.site_layer(site)      # raises for an unknown site
    if repo and not (Path(repo).expanduser() / ".git").exists():
        raise config.ConfigError(f"--repo {repo}: not a git checkout")
    keys: dict = {}
    if site:
        keys["site"] = site
    if repo:
        keys.setdefault("git", {})["repo"] = str(Path(repo).expanduser().resolve())
    if daemon_host:
        keys.setdefault("daemon", {})["host"] = daemon_host
    if port is not None:
        keys.setdefault("daemon", {})["port"] = int(port)
    elif existing is None:
        keys.setdefault("daemon", {})["port"] = default_port()
    if existing is None and os.environ.get("DRAMSIM3_HOME"):
        keys.setdefault("paths", {})["dramsim3"] = os.environ["DRAMSIM3_HOME"]
    keys = config.deep_merge(keys, extra or {})
    data = config.deep_merge(existing or {}, keys)
    errors = config.validate_user_layer(data)
    if errors:
        raise config.ConfigError("; ".join(errors))

    env_root = os.environ.get("FEATX_ROOT")
    if env_root and Path(os.path.abspath(Path(env_root).expanduser())) != target:
        raise config.ConfigError(f"FEATX_ROOT={env_root} overrides --root {target}; unset it")

    target.mkdir(parents=True, exist_ok=True)
    store.atomic_write_text(layer_path, yaml.safe_dump(data, sort_keys=False))
    pointer = None
    if not env_root:
        pointer = config.set_root_pointer(target)
    config.reload_settings()
    config.ensure_layout()
    from .sched.daemon import ensure_token
    ensure_token()
    return {"root": str(target), "user_layer": str(layer_path), "written": keys, "adopted": existing is not None,
            "site_detected": detected,
            "pointer": str(pointer) if pointer else None, "token": str(config.token_path())}


def next_steps() -> list[str]:
    s = config.settings()
    host = s["daemon"].get("host") or "<this host>"
    port = s["daemon"]["port"]
    mcp = Path(sys.executable).parent / "featx-mcp"
    return [
        "featx doctor                      # check paths, tools and ssh to the hosts",
        "featx daemon start                # featxd on " + host,
        f"claude mcp add featx -- {mcp}",
        f"UI: http://{host}:{port}/ui/   (laptop: ssh -N -L {port}:{host}:{port} <login-node>)",
        f"token: {config.token_path()}",
    ]
