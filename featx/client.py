"""HTTP client for featxd (used by the CLI and the MCP server). The daemon is found via
$FEATX_ROOT/daemon.json (or FEATX_URL); auth token from ~/.config/featx/token."""

from __future__ import annotations

import os
import time

import httpx

from . import config, store


class DaemonDown(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


def daemon_info() -> dict:
    return store.read_json(config.daemon_json(), {}) or {}


def daemon_alive(info: dict | None = None, max_age: float = 60.0) -> bool:
    info = info if info is not None else daemon_info()
    return bool(info) and not info.get("stopped") and time.time() - float(info.get("heartbeat", 0)) < max_age


def base_url() -> str:
    if os.environ.get("FEATX_URL"):
        return os.environ["FEATX_URL"].rstrip("/")
    info = daemon_info()
    if not daemon_alive(info):
        raise DaemonDown(f"featxd is not running for {config.root()} (start: featx daemon start)")
    return f"http://{info['host']}:{info['port']}"


class Client:
    def __init__(self, timeout: float = 60.0) -> None:
        self.url = base_url()
        tok = config.token_path().read_text().strip() if config.token_path().is_file() else ""
        self.http = httpx.Client(base_url=self.url, timeout=timeout, headers={"X-Featx-Token": tok},
                                 trust_env=False)

    def _do(self, method: str, path: str, *, timeout: float | None = None, **kw):
        try:
            r = self.http.request(method, path, timeout=timeout or self.http.timeout, **kw)
        except httpx.TransportError as error:
            raise DaemonDown(f"featxd unreachable at {self.url}: {error}") from error
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except ValueError:
                msg = r.text
            raise ApiError(msg)
        ctype = r.headers.get("content-type", "")
        return r.json() if "json" in ctype else r.text

    def get(self, path: str, timeout: float | None = None, **params):
        return self._do("GET", path, params={k: v for k, v in params.items() if v is not None}, timeout=timeout)

    def post(self, path: str, body: dict | None = None, timeout: float | None = None):
        return self._do("POST", path, json=body or {}, timeout=timeout)
