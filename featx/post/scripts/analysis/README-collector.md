# GSIM SPEC performance collection

`collect_gsim_spec_perf.py` parses current XiangShan `[PERF]` records by exact
counter name. It does not depend on the Chisel module hierarchy used by older
collection scripts.

## Usage

```bash
python3 collect_gsim_spec_perf.py \
  -r <suite-result-directory> \
  --config <suite-point-config.json> \
  --suite spec17 \
  -N 2 \
  -o <output-directory>
```

`-N` keeps the legacy meaning:

- `1`: use the first PERF timestamp block for a run without warmup.
- `2`: require two blocks and use the second, formal-run block.
- `3`: require two blocks and use the first, warmup block.

By default, a point is included only when `simulator_out.txt` contains
`GOOD TRAP` or `EXCEEDING CYCLE/INSTR LIMIT`.

## Outputs

- `points.csv`: raw counters and derived metrics for every configured point.
- `benchmarks.csv`: SimPoint-weighted benchmark metrics. IPC is calculated from
  weighted CPI and then inverted.
- `suite.json`: completion, suite summaries, and raw totals over completed points.

The main derived metrics are:

```text
IPC             = commitInstr / clock_cycle
branch MPKI     = resolve_branch_mispredicts * 1000 / commitInstr
WPT fraction    = bpu_train_wpt / (bpu_train_wpt + bpu_train_rpt)
lifecycle delta = branches - rpt - wpt - pending - duplicate - unclassified
```

The Topdown formulas follow the old Kunminghu collector. Counters renamed in the
current RTL are reconstructed explicitly from the corresponding IQ-full and
frontend/backend-other counters. A point is marked invalid if a required counter
is absent; missing counters are not silently treated as zero.
