"""Progress parsing and ETA.

The emu prints "Current Finished Inst Num N. Simulation Speed: X cycle/s Y inst/s" every
100k instructions (often without newlines in between). N counts warmup too, so the target
is I (-I is the total instruction limit; -W is the warmup part of it).
"""

from __future__ import annotations

import os
import re
import statistics
import time
from pathlib import Path

from .. import config, store

PROGRESS_RE = re.compile(rb"Current Finished Inst Num (\d+)\. Simulation Speed: (\d+) cycle/s (\d+) inst/s")
PROGRESS_TAIL = 64 * 1024


def parse_progress(path: Path) -> dict | None:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > PROGRESS_TAIL:
                handle.seek(size - PROGRESS_TAIL)
            data = handle.read()
    except OSError:
        return None
    hits = PROGRESS_RE.findall(data)
    if not hits:
        return None
    inst = int(hits[-1][0])
    rates = [int(h[2]) for h in hits[-20:] if int(h[2]) > 1]
    rate = statistics.median(rates) if rates else 0
    return {"inst": inst, "inst_per_s": rate, "mtime": os.path.getmtime(path)}


def job_remaining(progress: dict | None, target: int | None, started: float | None,
                  fallback: float | None) -> float | None:
    """Seconds left for one running job."""
    now = time.time()
    if progress and target and progress.get("inst_per_s"):
        left = max(target - progress["inst"], 0) / progress["inst_per_s"]
        return left
    if fallback is not None and started:
        return max(fallback - (now - started), 60.0)
    return fallback


def history_key(kind: str, job_name: str, spec: dict) -> str:
    if kind == "run":
        return f"run:{job_name}:{spec.get('W')}:{spec.get('I')}"
    if kind == "build":
        return f"build:{spec.get('type')}:{'relink' if (spec.get('gsim') or {}).get('reuse_build_from') else 'full'}"
    return f"post:{job_name}"


class History:
    def __init__(self) -> None:
        self._mtime = None
        self._data: dict[str, list[float]] = {}

    def _load(self) -> None:
        path = config.history_path()
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        data: dict[str, list[float]] = {}
        for rec in store.read_jsonl(path):
            if rec.get("ok"):
                data.setdefault(rec["key"], []).append(float(rec["duration"]))
                data.setdefault(rec["key"].rsplit(":", 2)[0] if rec["key"].startswith("run:") else rec["key"],
                                []).append(float(rec["duration"]))
        self._data = data

    def median(self, key: str) -> float | None:
        self._load()
        vals = self._data.get(key)
        if not vals and key.startswith("run:"):
            vals = self._data.get(key.rsplit(":", 2)[0])
        return statistics.median(vals[-30:]) if vals else None

    def generic_run(self) -> float | None:
        self._load()
        vals = [v for k, vs in self._data.items() if k.startswith("run:") and k.count(":") == 3 for v in vs]
        return statistics.median(vals) if vals else None

    def record(self, key: str, duration: float, ok: bool, **extra) -> None:
        store.append_jsonl(config.history_path(), {"time": store.now_iso(), "key": key,
                                                   "duration": round(duration, 1), "ok": ok, **extra})


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 86400 * 2:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"
