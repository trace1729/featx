"""Host selection. Reuses util/codex_dispatch.py's probe and score; adds capacity
accounting for jobs featx launched since the last probe (load average lags by minutes)."""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from .. import config

_cd = None


def codex_dispatch():
    global _cd
    if _cd is None:
        path = config.util_dir() / "codex_dispatch.py"
        spec = importlib.util.spec_from_file_location("featx_codex_dispatch", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod   # @dataclass resolves annotations via sys.modules[cls.__module__]
        spec.loader.exec_module(mod)
        _cd = mod
    return _cd


LOCAL_NAMES = {"localhost", "local", socket.gethostname().split(".")[0]}


def is_local(host: str) -> bool:
    return host in LOCAL_NAMES


def probe_local():
    cd = codex_dispatch()
    proc = subprocess.run(["python3", "-"], input=cd.REMOTE_PROBE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=10, check=False)
    return cd.ServerLoad.from_probe(socket.gethostname().split(".")[0], json.loads(proc.stdout))


@dataclass
class HostState:
    host: str
    load: object | None = None          # codex_dispatch.ServerLoad
    probed_at: float = 0.0
    error: str | None = None
    bad_until: float = 0.0
    launched_since_probe: list[tuple[float, int, float]] = field(default_factory=list)  # (t, cores, mem_gb)


class Placement:
    def __init__(self) -> None:
        self.hosts: dict[str, HostState] = {}
        self.lock = threading.Lock()

    def pool(self) -> list[str]:
        s = config.settings()
        hosts = list(s.get("hosts") or codex_dispatch().DEFAULT_HOSTS)
        excl = set(s.get("exclude_hosts") or [])
        return [h for h in hosts if h not in excl]

    def refresh(self, force: bool = False) -> None:
        pset = config.settings()["placement"]
        ttl, timeout = pset["probe_ttl"], pset["probe_timeout"]
        now = time.time()
        stale = [h for h in self.pool() if force or now - self.hosts.get(h, HostState(h)).probed_at > ttl]
        if not stale:
            return
        remote = [h for h in stale if not is_local(h)]
        loads, errors = ([], {})
        if remote:
            loads, errors = codex_dispatch().probe_all(remote, timeout)
        for h in stale:
            if is_local(h):
                try:
                    load = probe_local()
                    loads.append(load.__class__(**{**load.__dict__, "host": h}))
                except Exception as error:  # noqa: BLE001
                    errors[h] = str(error)
        with self.lock:
            for load in loads:
                st = self.hosts.setdefault(load.host, HostState(load.host))
                st.load, st.probed_at, st.error = load, now, None
                st.launched_since_probe.clear()
            for h, err in errors.items():
                st = self.hosts.setdefault(h, HostState(h))
                st.load, st.probed_at, st.error = None, now, err

    def mark_bad(self, host: str, seconds: float = 300) -> None:
        with self.lock:
            self.hosts.setdefault(host, HostState(host)).bad_until = time.time() + seconds

    def free(self, st: HostState, featx_jobs_on_host: int) -> tuple[float, float]:
        pset = config.settings()["placement"]
        if st.load is None or st.bad_until > time.time():
            return 0.0, 0.0
        if featx_jobs_on_host >= pset["max_jobs_per_host"]:
            return 0.0, 0.0
        ld = st.load
        busy = max(ld.load1, ld.cpu_busy * ld.cores)
        cores = ld.cores - pset["reserve_cores"] - busy - sum(c for _, c, _ in st.launched_since_probe)
        mem = ld.memory_available_kib / (1 << 20) - pset["mem_reserve_gb"] - sum(m for _, _, m in st.launched_since_probe)
        return cores, mem

    def choose(self, cores: int, mem_gb: float, jobs_per_host: dict[str, int],
               avoid: set[str] | None = None, pin: str | None = None) -> str | None:
        avoid = avoid or set()
        with self.lock:
            best, best_key = None, None
            cands = [pin] if pin else self.pool()
            for h in cands:
                if h in avoid and not pin:
                    continue
                st = self.hosts.get(h)
                if st is None:
                    continue
                fc, fm = self.free(st, jobs_per_host.get(h, 0))
                if fc < cores or fm < mem_gb:
                    continue
                key = (-(fc / st.load.cores), st.load.score, h)
                if best_key is None or key < best_key:
                    best, best_key = h, key
            if best:
                self.hosts[best].launched_since_probe.append((time.time(), cores, mem_gb))
            return best

    def snapshot(self, jobs_per_host: dict[str, int]) -> list[dict]:
        out = []
        with self.lock:
            for h in self.pool():
                st = self.hosts.get(h)
                if not st:
                    out.append({"host": h, "state": "unprobed"})
                    continue
                fc, fm = self.free(st, jobs_per_host.get(h, 0))
                row = {"host": h, "featx_jobs": jobs_per_host.get(h, 0), "free_cores": round(fc, 1),
                       "free_mem_gb": round(fm, 1), "probed_age": round(time.time() - st.probed_at),
                       "bad": st.bad_until > time.time()}
                if st.load:
                    row.update(cores=st.load.cores, load1=st.load.load1, cpu_busy=round(st.load.cpu_busy, 3),
                               score=round(st.load.score, 3))
                else:
                    row["error"] = st.error
                out.append(row)
        return out
