"""Stdlib post helper scripts run as the post steps run them (subprocess, no featx import)."""

import csv
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "featx" / "post" / "scripts"


def run(script, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def perf_dir(d, cycles, insts):
    d.mkdir(parents=True)
    rob = "TOP.SimTop.l_soc.core_with_l2.core.backend.inner.ctrlBlock.rob"
    lines = [f"[PERF ][time=   {t}] {rob}: {k},      {v}"
             for t in (100, 200) for k, v in (("clock_cycle", cycles), ("commitInstr", insts))]
    (d / "simulator_err.txt").write_text("\n".join(lines) + "\n")


def test_perf_keys_finds_shortened_weight_dirs(tmp_path):
    # old result roots spell weights shortened; manifests and featx use 6 digits
    perf_dir(tmp_path / "ref" / "GemsFDTD_10619_0.03475", 1000, 2000)
    perf_dir(tmp_path / "ref" / "bwaves_1_7_5.1e-05", 1000, 1000)
    perf_dir(tmp_path / "self" / "GemsFDTD_10619_0.034750", 1000, 1900)
    perf_dir(tmp_path / "self" / "bwaves_1_7_0.000051", 1000, 1000)
    pts = tmp_path / "points.txt"
    pts.write_text("GemsFDTD_10619_0.034750\nbwaves_1_7_0.000051\nmcf_1_0.100000\n")
    p = run("perf_keys.py", "--runs", f"exec={tmp_path / 'ref'}", f"self={tmp_path / 'self'}",
            "--points", pts, "-o", tmp_path / "out")
    assert p.returncode == 0, p.stderr
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["exec.finished"] == 2 and summary["common_points"] == 2
    assert abs(summary["self.mean_err_pct"] - (-2.5)) < 1e-6
    # a point with no dir at all is still reported missing
    assert "mcf_1_0.100000 | exec | missing: no PERF output" in (tmp_path / "out" / "report.md").read_text()


def write_points(path, rows):
    with open(path, "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["benchmark", "point", "status", "ipc"])
        w.writerows(rows)
    return path


def test_check_paired(tmp_path):
    ok = write_points(tmp_path / "ok.csv", [("a", "1_0.5", "ok", "1.2"), ("b", "1_0.5", "ok", "0.8")])
    iw = write_points(tmp_path / "iw.csv", [("a", "1_0.5", "parse_error:need 2 PERF blocks for -N 2, found 1", "")])
    other = write_points(tmp_path / "other.csv", [("c", "1_0.5", "ok", "1.0")])
    p = run("check_paired.py", ok, ok)
    assert p.returncode == 0 and "paired: 2" in p.stderr
    p = run("check_paired.py", iw, ok)
    assert p.returncode == 3
    assert p.stderr.startswith("check_paired: nothing to compare: the runset has no valid point")
    assert "0/1 points valid (invalid: parse_error:need 2 PERF blocks for -N 2, found 1 x1)" in p.stderr
    p = run("check_paired.py", ok, iw)
    assert p.returncode == 3 and "the reference has no valid point" in p.stderr
    p = run("check_paired.py", ok, other)
    assert p.returncode == 3 and "other point set / reference" in p.stderr
