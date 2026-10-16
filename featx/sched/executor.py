"""Launch/poll/kill jobs through featx/bin/featx-exec. A job dir holds .fx/<attempt>/ state:
cmd.sh, host, pid, start, heartbeat, end, exit_code (+ stdout_name/stderr_name)."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from .. import config, store
from . import hostprobe
from .placement import is_local

ARCHIVE_NAMES = ("simulator_out.txt", "simulator_err.txt", "log")


def fx_dir(job_dir: Path, attempt: int) -> Path:
    return job_dir / ".fx" / str(attempt)


def exec_path() -> Path:
    return config.BIN_DIR / "featx-exec"


def prepare(job_dir: Path, attempt: int, script: str, stdout_name: str = "log",
            stderr_name: str | None = None) -> Path:
    """Write cmd.sh for a new attempt; outputs of the previous attempt move into its .fx dir
    so collectors only ever see the current attempt."""
    job_dir.mkdir(parents=True, exist_ok=True)
    if attempt > 1:
        prev = fx_dir(job_dir, attempt - 1)
        prev.mkdir(parents=True, exist_ok=True)
        for name in ARCHIVE_NAMES:
            if (job_dir / name).exists():
                shutil.move(str(job_dir / name), str(prev / name))
    fx = fx_dir(job_dir, attempt)
    if fx.exists():
        shutil.rmtree(fx)
    fx.mkdir(parents=True)
    store.atomic_write_text(fx / "cmd.sh", script)
    (fx / "stdout_name").write_text(stdout_name + "\n")
    (fx / "stderr_name").write_text((stderr_name or stdout_name) + "\n")
    return fx


def ssh_base(host: str) -> list[str]:
    return hostprobe.ssh_base(host, 8)


def launch(host: str, job_dir: Path, attempt: int, env: dict | None = None) -> tuple[bool, str]:
    exports = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items())
    inner = (f"{exports} setsid nohup bash {shlex.quote(str(exec_path()))} "
             f"{shlex.quote(str(job_dir))} {attempt} </dev/null >/dev/null 2>&1 &")
    cmd = ["bash", "-c", inner] if is_local(host) else [*ssh_base(host), inner]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return False, "featx: launch failed: ssh timeout"
    if proc.returncode != 0:
        return False, f"featx: launch failed: rc={proc.returncode} {proc.stderr.strip()[-300:]}"
    return True, ""


def read_state(job_dir: Path, attempt: int) -> dict:
    fx = fx_dir(job_dir, attempt)
    out = {}
    for name in ("host", "pid", "start", "heartbeat", "end", "exit_code"):
        try:
            out[name] = (fx / name).read_text().strip()
        except (FileNotFoundError, OSError):
            pass
    return out


def kill(host: str, pid: str | int, signal: str = "TERM") -> bool:
    """Kill every process of the job. featx-exec runs as a session leader (setsid), but children
    may leave its process group (`timeout` calls setpgid), so match the session, not the pgid."""
    pid = int(pid)
    inner = (f"pkill -{signal} -s {pid} 2>/dev/null; kill -{signal} -- -{pid} 2>/dev/null; "
             f"kill -{signal} {pid} 2>/dev/null; true")
    cmd = ["bash", "-c", inner] if is_local(host) else [*ssh_base(host), inner]
    try:
        return subprocess.run(cmd, timeout=20, check=False,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _run_on(host: str, inner: str, timeout: float) -> subprocess.CompletedProcess | None:
    cmd = ["bash", "-c", inner] if is_local(host) else [*ssh_base(host), inner]
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None


def kill_many(host: str, pids: list[int], signal: str = "TERM") -> bool:
    """kill() for many jobs of one host in a single ssh (a 125-point cancel was 125 serial sshs)."""
    inner = "; ".join(f"pkill -{signal} -s {int(p)} 2>/dev/null; kill -{signal} -- -{int(p)} 2>/dev/null; "
                      f"kill -{signal} {int(p)} 2>/dev/null" for p in pids) + "; true"
    proc = _run_on(host, inner, 30)
    return proc is not None and proc.returncode == 0


def alive_many(host: str, pids: list[int]) -> dict[int, bool | None]:
    """alive_on_host() for many pids of one host in a single ssh (None = host unreachable)."""
    inner = "; ".join(f"kill -0 {int(p)} 2>/dev/null && echo {int(p)} alive || echo {int(p)} gone" for p in pids)
    proc = _run_on(host, inner, 30)
    if proc is None or proc.returncode != 0:
        return {int(p): None for p in pids}
    seen = dict(line.split() for line in proc.stdout.splitlines() if len(line.split()) == 2)
    return {int(p): (seen[str(int(p))] == "alive") if str(int(p)) in seen else None for p in pids}


def alive_on_host(host: str, pid: str | int) -> bool | None:
    """Direct check used before declaring a stale-heartbeat job dead (None = host unreachable)."""
    # Only the featx-exec leader counts: without it nobody writes exit_code, so a surviving emu
    # is an orphan to kill (kill() covers the whole session), not a live job.
    inner = f"kill -0 {int(pid)} 2>/dev/null && echo alive || echo gone"
    cmd = ["bash", "-c", inner] if is_local(host) else [*ssh_base(host), inner]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                              timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() == "alive"
