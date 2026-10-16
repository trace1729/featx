"""Host load probe over ssh (vendored from toolsets/util/codex_dispatch.py, 05b46eb): one
python3 snippet sent through stdin reports cores, load average, CPU busy fraction and memory.
The host pool is featx.yaml `hosts`."""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
from dataclasses import dataclass


SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectionAttempts=1",
    "-o",
    "ConnectTimeout=3",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=3",
)

# Sent through stdin so it does not depend on this file being visible remotely.
REMOTE_PROBE = r"""
import json
import os
import time

def cpu_times():
    with open("/proc/stat", encoding="ascii") as handle:
        values = [int(value) for value in handle.readline().split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle

def memory():
    values = {}
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            values[key] = int(value.split()[0])
    total = values["MemTotal"]
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return total, available

first_total, first_idle = cpu_times()
time.sleep(0.20)
second_total, second_idle = cpu_times()
delta = max(second_total - first_total, 1)
cpu_busy = 1.0 - (second_idle - first_idle) / delta
load1, load5, load15 = os.getloadavg()
cores = len(os.sched_getaffinity(0))
memory_total, memory_available = memory()
print(json.dumps({
    "cores": cores,
    "load1": load1,
    "load5": load5,
    "load15": load15,
    "cpu_busy": max(0.0, min(cpu_busy, 1.0)),
    "memory_total_kib": memory_total,
    "memory_available_kib": memory_available,
}))
"""


@dataclass(frozen=True)
class ServerLoad:
    host: str
    cores: int
    load1: float
    load5: float
    load15: float
    cpu_busy: float
    memory_total_kib: int
    memory_available_kib: int
    score: float

    @classmethod
    def from_probe(cls, host: str, values: dict[str, object]) -> "ServerLoad":
        cores = int(values["cores"])
        memory_total = int(values["memory_total_kib"])
        if cores <= 0 or memory_total <= 0:
            raise ValueError("invalid core or memory count")
        load1 = float(values["load1"])
        cpu_busy = float(values["cpu_busy"])
        memory_available = int(values["memory_available_kib"])
        memory_used = 1.0 - memory_available / memory_total

        # CPU pressure dominates; memory breaks ties and avoids nearly full hosts.
        score = 0.55 * (load1 / cores) + 0.40 * cpu_busy + 0.05 * memory_used
        return cls(
            host=host,
            cores=cores,
            load1=load1,
            load5=float(values["load5"]),
            load15=float(values["load15"]),
            cpu_busy=cpu_busy,
            memory_total_kib=memory_total,
            memory_available_kib=memory_available,
            score=score,
        )


def ssh_base(host: str, timeout: float) -> list[str]:
    options = list(SSH_OPTIONS)
    options[5] = f"ConnectTimeout={max(1, int(timeout))}"
    return ["ssh", *options, host]


def probe(host: str, timeout: float) -> tuple[ServerLoad | None, str | None]:
    try:
        result = subprocess.run(
            [*ssh_base(host, timeout), "python3", "-"],
            input=REMOTE_PROBE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    if result.returncode != 0:
        error = result.stderr.strip() or f"ssh exited with {result.returncode}"
        return None, error.splitlines()[-1]
    try:
        return ServerLoad.from_probe(host, json.loads(result.stdout)), None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return None, f"invalid probe output: {error}"


def probe_all(
    hosts: list[str], timeout: float
) -> tuple[list[ServerLoad], dict[str, str]]:
    loads: list[ServerLoad] = []
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        futures = {executor.submit(probe, host, timeout): host for host in hosts}
        for future in concurrent.futures.as_completed(futures):
            host = futures[future]
            load, error = future.result()
            if load is not None:
                loads.append(load)
            else:
                errors[host] = error or "unknown error"
    loads.sort(key=lambda item: (item.score, item.load5 / item.cores, item.host))
    return loads, errors
