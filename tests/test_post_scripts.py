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


def point(d, cycles, insts, out="EXCEEDING CYCLE/INSTR LIMIT", err_extra=""):
    perf_dir(d, cycles, insts)
    (d / "simulator_out.txt").write_text(f"boot\n{out}\n")
    if err_extra:
        with open(d / "simulator_err.txt", "a") as handle:
            handle.write(err_extra + "\n")


def test_spec_score_matches_perf_trigger_formula(tmp_path):
    # spec06 groups by short-name prefix: astar = astar_biglakes + astar_rivers
    insts = {"astar_biglakes": {"insts": "2000000000", "points": {"1": "0.6", "2": "0.4"}},
             "astar_rivers": {"insts": "1000000000", "points": {"5": "1.0"}}}
    (tmp_path / "insts.json").write_text(json.dumps(insts))
    pts = tmp_path / "points.txt"
    pts.write_text("astar_biglakes_1_0.600000\nastar_biglakes_2_0.400000\nastar_rivers_5_1.000000\n")
    root = tmp_path / "self"
    point(root / "astar_biglakes_1_0.600000", 2000, 1000)            # cpi 2
    point(root / "astar_biglakes_2_0.400000", 1000, 1000, out="ABORT at pc 0x0")
    point(root / "astar_rivers_5_1.000000", 1500, 1000, out="HIT GOOD TRAP")   # cpi 1.5
    ref = tmp_path / "ref"
    for name in ("astar_biglakes_1_0.6", "astar_biglakes_2_0.4", "astar_rivers_5_1.0"):  # shortened
        point(ref / name, 1000, 1000)                                  # cpi 1
    p = run("spec_score.py", "--runs", f"self={root}", f"exec={ref}", "--points", pts,
            "--insts", tmp_path / "insts.json", "--label", "spec06", "--frequency", "2", "-o", tmp_path / "out")
    assert p.returncode == 0, p.stderr
    s = json.loads((tmp_path / "out" / "summary.json").read_text())
    # biglakes: only the cpi-2 point counts -> 2 * 2e9 / 2e9 = 2 s; rivers: 1.5 * 1e9 / 2e9 = 0.75 s
    time = 2 + 0.75
    assert abs(s["score.473.astar"] - 7020 / time / 2) < 1e-9
    assert abs(s["min_coverage"] - (0.6 * 2e9 + 1.0 * 1e9) / 3e9) < 1e-12
    assert s["finished"] == 2 and s["exec.finished"] == 3 and s["common_points"] == 2
    # every other group has no benchmark: the suite score is null, as perf_trigger's nan
    assert s["spec_per_ghz"] is None and s["groups_scored"] == 1
    assert "403.gcc" in s["missing_groups"]
    assert abs(s["spec_per_ghz_available"] - 7020 / time / 2) < 1e-9
    # the baseline finished every point at cpi 1: 1 s + 0.5 s
    assert abs(s["exec.spec_per_ghz_available"] - 7020 / (1 + 0.5) / 2) < 1e-9
    assert "473.astar" in (tmp_path / "out" / "report.md").read_text()


def test_spec_score_assertion_and_missing_insts(tmp_path):
    (tmp_path / "insts.json").write_text(json.dumps({"mcf": {"insts": "100", "points": {"1": "1.0"}}}))
    pts = tmp_path / "points.txt"
    pts.write_text("mcf_1_1.000000\n")
    point(tmp_path / "r" / "mcf_1_1.000000", 10, 10, out="still running",
          err_extra="emu: Assertion failed")
    p = run("spec_score.py", "--runs", f"self={tmp_path / 'r'}", "--points", pts,
            "--insts", tmp_path / "insts.json", "--version", "06", "-o", tmp_path / "out")
    assert p.returncode == 0, p.stderr
    rows = list(csv.DictReader(open(tmp_path / "out" / "benchmarks.csv")))
    assert rows[0]["states"] == "aborted:1"
    pts.write_text("gcc_1_1.000000\n")
    p = run("spec_score.py", "--runs", f"self={tmp_path / 'r'}", "--points", pts,
            "--insts", tmp_path / "insts.json", "-o", tmp_path / "out")
    assert p.returncode != 0 and "no insts for gcc" in p.stderr
