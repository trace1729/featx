import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from featx import config


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Isolated data root + worktree root + token; fast liveness settings."""
    r = tmp_path / "featx"
    r.mkdir()
    monkeypatch.setenv("FEATX_ROOT", str(r))
    monkeypatch.setenv("FEATX_WT_ROOT", str(tmp_path / "wt"))
    monkeypatch.setenv("FEATX_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("FEATX_DIRECT", "1")
    (r / "featx.yaml").write_text(yaml.safe_dump({
        "hosts": ["localhost"],
        "trace_enabled": True,  # Most integration tests exercise trace; opt in explicitly.
        "liveness": {"heartbeat_interval": 1, "dead_after": 4, "launch_grace": 15},
        "retry": {"max_infra_attempts": 1, "max_design_relaunches": 2},
    }))
    config.reload_settings()
    config.ensure_layout()
    yield r
    config.reload_settings()


def update_layer(root, **keys):
    """Deep-merge keys into the test user layer $FEATX_ROOT/featx.yaml and reload."""
    path = root / "featx.yaml"
    data = config.deep_merge(yaml.safe_load(path.read_text()) or {}, keys)
    path.write_text(yaml.safe_dump(data))
    config.reload_settings()
    return data


@pytest.fixture
def nfs_site(root, tmp_path):
    """The committed nfs-share site profile (workloads spec06/17/26, shared baselines) with the
    test hosts; nothing under /nfs is read unless a test resolves points."""
    update_layer(root, site="nfs-share", hosts=["localhost"], analysis_dir=str(tmp_path / "analysis"),
                 workloads={"spec06": {"reference": "exec-reference"},
                            "spec17": {"reference": "exec-reference-spec17"}},
                 baselines={"exec-reference": {"root": "/shared/baselines/spec06/exec",
                                               "checkpoint": "spec06_gcc15_rv64gcb_base_260122"},
                            "exec-reference-spec17": {"root": "/shared/baselines/spec17/exec",
                                                      "checkpoint": "spec17_rv64gcb_o2_20m"}})
    return root


# The pre-portability package defaults of a legacy installation: `featx init --adopt` writes
# these into that user layer, and bincache keys of existing builds must not change.
MIGRATED = {
    "site": "nfs-share",
    "share": "/nfs/share",
    "paths": {"dramsim3": "/opt/DRAMsim3",
              "verilator_bin": "/opt/verilator/bin"},
}


@pytest.fixture
def migrated(root):
    update_layer(root, **MIGRATED)
    return root


FAKE_EMU = r'''#!/usr/bin/env bash
# fake emu: behaviour chosen by the bench name in the trace (or exec checkpoint) path
echo "$*" > argv.txt
trace=; W=0; I=0
while (($#)); do
  case "$1" in
    --tracertl-file|-i) trace=$2; shift 2 ;;
    -W) W=$2; shift 2 ;;
    -I) I=$2; shift 2 ;;
    *) shift ;;
  esac
done
perf() {  # $1 = time stamp, $2 = cycles, $3 = insts
  echo "[PERF ][time=   $1] TOP.SimTop.l_soc.core_with_l2.core.backend.inner.ctrlBlock.rob: clock_cycle,      $2" >&2
  echo "[PERF ][time=   $1] TOP.SimTop.l_soc.core_with_l2.core.backend.inner.ctrlBlock.rob: commitInstr,      $3" >&2
}
case "$trace" in
  */ok/*)
    for n in 100000 200000 300000; do echo "Current Finished Inst Num $n. Simulation Speed: 5000 cycle/s 4000 inst/s"; done
    perf 100 1000 400; perf 200 2000 1000
    echo "EXCEEDING CYCLE/INSTR LIMIT"; exit 0 ;;
  */abort/*)
    echo "Current Finished Inst Num 100000. Simulation Speed: 5000 cycle/s 4000 inst/s"
    echo "TraceRTL Error at inst 77" >&2; exit 1 ;;
  */hang/*)
    echo "Current Finished Inst Num 100000. Simulation Speed: 5000 cycle/s 4000 inst/s"
    sleep 600; exit 0 ;;
esac
echo "unknown trace $trace" >&2; exit 3
'''


@pytest.fixture
def fake_emu(tmp_path):
    p = tmp_path / "fake-emu"
    p.write_text(FAKE_EMU)
    p.chmod(0o755)
    traces = tmp_path / "traces"
    for bench in ("ok", "abort", "hang"):
        d = traces / bench / "1"
        d.mkdir(parents=True)
        (d / "_1_0.5_.trace.zstd").write_text("x")
    return p, traces
