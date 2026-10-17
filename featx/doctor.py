"""`featx doctor`: check that what the configuration points at exists and is usable.

Each check gives ok / warn / fail. fail = featx can't work (data root, the site profile);
warn = a feature that needs it is off (worktrees without git.repo, exec mode without the NEMU
diff .so, pair/diagnose without the XiangShan top-down configs, ...)."""

from __future__ import annotations

import concurrent.futures
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from . import config

OK, WARN, FAIL = "ok", "warn", "fail"


def _item(name: str, status: str, detail: str = "") -> dict:
    return {"check": name, "status": status, "detail": detail}


def _tool(name: str, value: str | None, bin_dir: str | None = None) -> tuple[str, str]:
    """A tool given as a path or a PATH name."""
    if value and "/" in value:
        return (OK, value) if os.access(value, os.X_OK) else (FAIL, f"{value}: not an executable")
    path = os.environ.get("PATH", "")
    if bin_dir:
        path = f"{bin_dir}:{path}"
    found = shutil.which(value or name, path=path)
    return (OK, found) if found else (FAIL, f"{value or name} not in PATH")


def _path(name: str, value, want: str = "file", why: str = "", missing: str = WARN) -> dict:
    if not value:
        return _item(name, missing, f"unset{' (' + why + ')' if why else ''}")
    p = Path(str(value))
    exists = p.is_file() if want == "file" else p.is_dir()
    return _item(name, OK if exists else missing, str(p) if exists else f"{p}: no such {want}")


def _writable(p: Path) -> bool:
    while not p.exists():
        p = p.parent
    return os.access(p, os.W_OK)


def local_checks() -> list[dict]:
    s = config.settings()
    paths = s.get("paths") or {}
    out = []
    root = config.root()
    out.append(_item("data root", OK if _writable(root) else FAIL, str(root)))
    site = s.get("site")
    found = None if site else config.detect_site()
    out.append(_item("site profile", OK if site else WARN, str(config.site_path(site)) if site else
                     (f"none, but this host has the {found} storage: featx config set site={found}" if found else
                      "none: hosts, workloads, checkpoints and baselines are all unset (featx config set "
                      "site=<name>; known: " + (", ".join(config.sites()) or "none") + ")")))
    tok = config.token_path()
    out.append(_item("api token", OK if tok.is_file() else WARN, str(tok) if tok.is_file() else
                     f"{tok} missing (featx init or featx daemon start creates it)"))
    repo = (s.get("git") or {}).get("repo")
    out.append(_item("git.repo", OK if repo and (Path(repo) / ".git").exists() else WARN,
                     repo if repo and (Path(repo) / ".git").exists() else
                     f"{repo or 'unset'}: node worktrees need the XiangShan checkout (featx config set git.repo=...)"))
    wt = config.wt_root()
    out.append(_item("wt_root", OK if _writable(wt) else FAIL, str(wt)))
    st, detail = _tool("gsim", paths.get("gsim"))
    out.append(_item("paths.gsim", st if st == OK else WARN, detail + ("" if st == OK else " (build.tool=gsim)")))
    st, detail = _tool("verilator", None, paths.get("verilator_bin"))
    out.append(_item("verilator", st if st == OK else WARN, detail + ("" if st == OK else " (build.tool=verilator)")))
    dram = paths.get("dramsim3")
    out.append(_item("paths.dramsim3", OK if dram and (Path(dram) / "build").is_dir() else WARN,
                     dram if dram and (Path(dram) / "build").is_dir() else
                     f"{dram or 'unset'}: needs <dir>/build (builds with options.dramsim3)"))
    out.append(_path("paths.nemu_diff", paths.get("nemu_diff"),
                     why=f"no {config.NEMU_SO} in git.repo either; exec runs use --no-diff"))
    out.append(_path("paths.gcpt_restorer", paths.get("gcpt_restorer"), why="exec runs of .gz checkpoints"))
    out.append(_path("paths.perf_trigger", paths.get("perf_trigger"), why="run.executor=perf_trigger"))
    tools = config.analysis_dir()
    out.append(_item("analysis scripts", OK if (tools / "collect_gsim_spec_perf.py").is_file() else FAIL,
                     f"{tools}{'' if s.get('analysis_dir') else ' (shipped with featx)'}"
                     if (tools / "collect_gsim_spec_perf.py").is_file() else
                     f"{tools}: no collect_gsim_spec_perf.py (featx.yaml analysis_dir)"))
    xs = paths.get("xiangshan") or repo
    cfg = Path(xs) / "scripts" / "top-down" / "configs.py" if xs else None
    out.append(_path("paths.xiangshan top-down", cfg, why="pair / diagnose TopDown definitions"))
    out.extend(checkpoint_checks(s))
    from . import ckpt
    known = ckpt.set_dirs()
    for name, wl in (s.get("workloads") or {}).items():
        wl = wl or {}
        out.append(_path(f"workload {name}: manifest", wl.get("manifest")))
        if wl.get("checkpoint"):
            out.append(_item(f"workload {name}: checkpoint", OK if wl["checkpoint"] in known else WARN,
                             wl["checkpoint"] if wl["checkpoint"] in known else
                             f"{wl['checkpoint']}: not a set under checkpoints.roots"))
        run = wl.get("run") or {}
        if run.get("trace_root"):
            out.append(_path(f"workload {name}: trace_root", run["trace_root"], "dir"))
        insts = wl.get("insts")
        if insts:
            out.append(_path(f"workload {name}: insts", insts if str(insts).startswith("/")
                             else config.DEFAULTS_DIR / insts))
    from . import baselines
    for name, b in baselines.load().items():
        out.append(_path(f"baseline {name}", b.get("root"), "dir"))
    return out


def checkpoint_checks(s: dict) -> list[dict]:
    """Each checkpoints root, with how many of its sets exec runs can use."""
    from . import ckpt
    roots = (s.get("checkpoints") or {}).get("roots") or []
    if not roots:
        return [_item("checkpoints.roots", WARN, "unset (exec runs only get the workloads' own checkpoints)")]
    rows = ckpt.catalog()
    out = []
    for r in roots:
        if not Path(r).is_dir():
            out.append(_item(f"checkpoints root {r}", WARN, f"{r}: no such dir"))
            continue
        mine = [x for x in rows if Path(x["path"]).parent == Path(r)]
        good = sum(x["runnable"] for x in mine)
        multi = sum(bool(x["multihart"]) for x in mine)
        broken = sum(not x["runnable"] and not x["multihart"] for x in mine)
        out.append(_item(f"checkpoints root {r}", OK, f"{len(mine)} sets: {good} runnable, {multi} multi-hart, "
                         f"{broken} broken (featx ckpt list --all)"))
    return out


def host_checks(hosts: list[str] | None = None, timeout: float = 8) -> list[dict]:
    """ssh to each host (BatchMode) and check that it sees the featx install and the data root
    at the same paths (jobs run featx-exec from there and write into the root)."""
    from .sched import hostprobe
    from .sched.placement import is_local
    s = config.settings()
    hosts = hosts if hosts is not None else [h for h in (s.get("hosts") or []) if h not in (s.get("exclude_hosts") or [])]
    daemon_host = (s.get("daemon") or {}).get("host")
    if daemon_host and daemon_host not in hosts:
        hosts = [*hosts, daemon_host]
    exe, root = config.BIN_DIR / "featx-exec", config.root()
    probe = f"test -x {shlex.quote(str(exe))} || echo 'no featx install'; test -d {shlex.quote(str(root))} || echo 'no data root'"

    def check(host: str) -> dict:
        if is_local(host):
            return _item(f"host {host}", OK, "local")
        try:
            proc = subprocess.run([*hostprobe.ssh_base(host, timeout), probe], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            return _item(f"host {host}", FAIL, "ssh timed out")
        if proc.returncode != 0:
            return _item(f"host {host}", FAIL, f"ssh failed: {(proc.stderr or '').strip()[-200:]}")
        missing = proc.stdout.strip()
        return _item(f"host {host}", FAIL if missing else OK,
                     missing.replace("\n", ", ") + f" ({exe.parent}, {root})" if missing else "ssh ok, install + root visible")

    if not hosts:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(hosts))) as pool:
        return list(pool.map(check, hosts))


def run(ssh: bool = True) -> dict:
    checks = local_checks() + (host_checks() if ssh else [])
    counts = {k: sum(c["status"] == k for c in checks) for k in (OK, WARN, FAIL)}
    return {"checks": checks, "counts": counts, "ok": counts[FAIL] == 0}
