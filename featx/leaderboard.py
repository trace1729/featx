"""Leaderboard: rank runsets by any post summary metric, only within a comparability class.

A metric is "<post step>.<summary key>", e.g. pk.self.mean_abs_err_pct. Two runsets are only
ranked against each other when they agree on the step's reference (baseline), PERF block,
run window (W, I) and point set: a 1M+2M smoke compared with a 20M+20M baseline is not
comparable with a 20M+20M run, even though both carry the same metric name.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import points as pts
from . import store, taskstore
from .model import Runset
from .tree import Entry, Tree

_cache: dict[str, tuple[tuple, dict]] = {}


def _signature(rdir: Path) -> tuple:
    files = [rdir / "experiment.yaml", rdir / "run" / "points.json"]
    pdir = rdir / "post"
    if pdir.is_dir():
        files += sorted(pdir.glob("*/summary.json"))
    sig = []
    for f in files:
        try:
            sig.append((str(f), f.stat().st_mtime_ns))
        except OSError:
            pass
    return tuple(sig)


def _point_set(rdir: Path, rs: Runset) -> list[str]:
    data = store.read_json(rdir / "run" / "points.json")
    if isinstance(data, list):
        return sorted(p if isinstance(p, str) else p.get("name", "") for p in data)
    if isinstance(data, dict):   # perf_trigger suite format {bench: {points: {cp: weight}}}
        return sorted(f"{b}_{cp}_{w}" for b, info in data.items() if isinstance(info, dict)
                      for cp, w in (info.get("points") or {}).items())
    try:
        return sorted(pts.resolve(rs.recipes.get("run") or {}))
    except Exception:  # noqa: BLE001 - a bad snapshot must not break the board
        return []


def _runset_record(entry: Entry, rs: Runset) -> dict:
    rdir = entry.dir / rs.name
    sig = _signature(rdir)
    hit = _cache.get(str(rdir))
    if hit and hit[0] == sig:
        return hit[1]
    run = rs.recipes.get("run") or {}
    plist = _point_set(rdir, rs)
    posts = {}
    pdir = rdir / "post"
    if pdir.is_dir():
        for step in sorted(pdir.iterdir()):
            s = store.read_json(step / "summary.json")
            if isinstance(s, dict):
                posts[step.name] = s
    rec = {"rdir": str(rdir), "W": run.get("W"), "I": run.get("I"), "points": plist,
           "points_hash": hashlib.sha1(json.dumps(plist).encode()).hexdigest()[:10] if plist else "none",
           "posts": posts}
    _cache[str(rdir)] = (sig, rec)
    return rec


def _fmt_count(n) -> str:
    if not isinstance(n, (int, float)):
        return str(n)
    for div, suf in ((1_000_000, "M"), (1_000, "k")):
        if n >= div and n % div == 0:
            return f"{int(n // div)}{suf}"
    return str(n)


def class_of(step: str, summary: dict, rec: dict) -> dict:
    ref = summary.get("reference", "-")
    block = summary.get("block", "-")
    key = f"{step}|ref={ref}|blk={block}|W={rec['W']}|I={rec['I']}|pts={rec['points_hash']}"
    label = (f"{step} vs {ref} · W={_fmt_count(rec['W'])} I={_fmt_count(rec['I'])} · "
             f"{len(rec['points'])} pts")
    return {"key": key, "label": label, "step": step, "reference": ref, "block": block,
            "W": rec["W"], "I": rec["I"], "n_points": len(rec["points"]), "points_hash": rec["points_hash"]}


def _complete(summary: dict, task_states: list[str]) -> bool:
    if "points" in summary and "common_points" in summary:
        return summary["common_points"] == summary["points"] and summary["points"] > 0
    return bool(task_states) and all(s == "succeeded" for s in task_states)


def default_order(metric: str) -> str:
    return "asc" if "err" in metric or "miss" in metric or metric.endswith("_s") else "desc"


def build(*, metric: str | None = None, order: str | None = None, cls: str | None = None,
          node: str | None = None, best_per_node: bool = False, include_incomplete: bool = False) -> dict:
    tree = Tree()
    tasks = taskstore.load_all()
    candidates = []          # (entry, rs, rec)
    metrics: dict[str, int] = {}
    for entry in tree.by_rel.values():
        if node and not (entry.rel == node or entry.rel.startswith(node.rstrip("/") + "/")):
            continue
        for rs in tree.runsets(entry):
            rec = _runset_record(entry, rs)
            candidates.append((entry, rs, rec))
            for step, summary in rec["posts"].items():
                for k, v in summary.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "block":
                        metrics[f"{step}.{k}"] = metrics.get(f"{step}.{k}", 0) + 1
    if not metric:
        pref = [m for m in metrics if m.endswith(".self.mean_abs_err_pct")]
        metric = max(pref, key=lambda m: metrics[m]) if pref else \
            (max(metrics, key=lambda m: metrics[m]) if metrics else None)
    order = order or (default_order(metric) if metric else "asc")
    rows: list[dict] = []
    classes: dict[str, dict] = {}
    if metric:
        step, _, key = metric.partition(".")
        for entry, rs, rec in candidates:
            summary = rec["posts"].get(step)
            if not summary or not isinstance(summary.get(key), (int, float)):
                continue
            states = [tasks[t].state for t in rs.tasks if t in tasks]
            c = class_of(step, summary, rec)
            info = classes.setdefault(c["key"], {**c, "rows": 0, "complete_rows": 0})
            complete = _complete(summary, states)
            info["rows"] += 1
            info["complete_rows"] += int(complete)
            rows.append({
                "runset": f"{entry.rel}/{rs.name}", "node": entry.rel, "name": rs.name,
                "created": rs.created, "purpose": rs.purpose, "sealed": rs.sealed,
                "verdict": entry.node.verdict, "node_status": entry.node.status,
                "bin": (rs.bin or {}).get("label") or (rs.bin or {}).get("key"),
                "value": summary[key], "class": c["key"], "complete": complete,
                "points": summary.get("points"), "common_points": summary.get("common_points"),
                "tasks": {tasks[t].kind: tasks[t].state for t in rs.tasks if t in tasks},
            })
    if cls is None and classes:
        cls = max(classes.values(), key=lambda c: (c["complete_rows"], c["rows"]))["key"]
    shown = [r for r in rows if r["class"] == cls and (include_incomplete or r["complete"])]
    shown.sort(key=lambda r: r["value"], reverse=(order == "desc"))
    if best_per_node:
        seen, best = set(), []
        for r in shown:
            if r["node"] not in seen:
                seen.add(r["node"])
                best.append(r)
        shown = best
    rank = 0
    for r in shown:
        if r["complete"]:
            rank += 1
            r["rank"] = rank
        else:
            r["rank"] = None
    return {"metric": metric, "order": order, "class": cls,
            "metrics": [{"name": m, "runsets": n} for m, n in sorted(metrics.items())],
            "classes": sorted(classes.values(), key=lambda c: -c["rows"]),
            "rows": shown}
