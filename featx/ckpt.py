"""Checkpoint catalog: the SimPoint checkpoint sets under featx.yaml `checkpoints.roots`.

A set is a sub-directory of a root whose name contains spec<NN> (spec06_gcc16_rva23_novec_260820,
legacy/spec17_rv64gcb_o2_20m, ...); the name is its version. Layouts, in order of preference:

  modern    checkpoint/<bench>/<cp>/_<cp>_<w>[_memory_].zstd   + json/checkpoints_all.json
  legacy0   checkpoint-0-0-0/<bench>/<cp>/_<cp>_<w>_.zstd      + checkpoint-0-0-0/cluster-0-0.json
  take_cpt  take_cpt/<bench>_<cp>_<w>/0/_<cp>_.gz  or  take_cpt/<bench>/<cp>/_<cp>_<w>_.gz
            + simpoint_summary.json / json/simpoint_summary.json (weights only, no insts)

The weights json ({bench: {insts, points: {cp: w}}} or {bench: {cp: w}}) is authoritative for
the points; each needs an image file (_<cp>_* in its dir; .zstd/.zst/.gz/.bin). Without a json
the images found are the points. A point is named <bench>_<cp>_<w6> (w6 = the weight with 6
decimals, as in the image names, the trace manifests and perf_trigger result dirs).

Multi-hart sets (manifest/<bench>.json "multihart": true, or "<n>harts" in the name) and broken
sets (dangling take_cpt symlink, no known layout) are listed but can't be run. An index is
cached per set under $FEATX_ROOT/cache/ckpt/ and rebuilt when the set dir, its weights json or
its image dir changes (`featx ckpt rescan` forces it)."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
from pathlib import Path

from . import config, store

INDEX_VERSION = 1
IMAGE_SUFFIXES = (".zstd", ".zst", ".gz", ".bin")
_SPEC = re.compile(r"spec(\d\d)")
_HARTS = re.compile(r"(?<![a-z0-9])\d+harts?(?![a-z])|dual_core")
_IMAGE = re.compile(r"^_(?P<cp>[^_]+)_(?P<w>[0-9.eE+-]*)_?(?:memory_)?\.(?:zstd|zst|gz|bin)$")


class CkptError(ValueError):
    pass


def roots() -> list[Path]:
    return [Path(r) for r in (config.settings().get("checkpoints") or {}).get("roots") or []]


def cache_dir() -> Path:
    return config.root() / "cache" / "ckpt"


def w6(weight: str) -> str:
    return f"{float(weight):.6f}"


# ------------------------------------------------------------------ discovery
def set_dirs() -> dict[str, Path]:
    """name -> dir, over all roots (the first root with a name wins)."""
    out: dict[str, Path] = {}
    for r in roots():
        try:
            entries = sorted(os.scandir(r), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if _SPEC.search(e.name) and e.is_dir() and e.name not in out:
                out[e.name] = Path(e.path)
    return out


def _weights_json(d: Path) -> Path | None:
    for rel in ("json/checkpoints_all.json", "checkpoint-0-0-0/cluster-0-0.json",
                "simpoint_summary.json", "json/simpoint_summary.json"):
        if (d / rel).is_file():
            return d / rel
    others = sorted((d / "json").glob("*.json")) if (d / "json").is_dir() else []
    return others[0] if len(others) == 1 else None


def _layout(d: Path) -> tuple[str | None, Path | None, str]:
    """(layout, image base dir, error)."""
    if (d / "json" / "checkpoints_all.json").is_file() and (d / "checkpoint").is_dir():
        return "modern", d / "checkpoint", ""
    if (d / "checkpoint-0-0-0").is_dir():
        return "legacy0", d / "checkpoint-0-0-0", ""
    tc = d / "take_cpt"
    if tc.is_dir():
        return "take_cpt", tc, ""
    if tc.is_symlink():
        return None, None, f"take_cpt -> {os.readlink(tc)}: dangling symlink"
    if (d / "checkpoint").is_dir():
        return "modern", d / "checkpoint", ""
    return None, None, "no known layout (checkpoint/, checkpoint-0-0-0/ or take_cpt/)"


def _multihart(d: Path) -> bool:
    if _HARTS.search(d.name):
        return True
    man = d / "manifest"
    try:
        first = next((p for p in sorted(man.iterdir()) if p.suffix == ".json"), None) if man.is_dir() else None
    except OSError:
        first = None
    if first:
        try:
            return bool(json.loads(first.read_text()).get("multihart"))
        except (OSError, ValueError, AttributeError):
            return False
    return False


def _image_in(dirpath: Path, cp: str) -> tuple[str, str] | None:
    """(image path, weight from its name or "") of checkpoint `cp` in `dirpath`."""
    try:
        names = sorted(os.listdir(dirpath))
    except OSError:
        return None
    for n in names:
        if n.startswith(f"_{cp}_") and n.endswith(IMAGE_SUFFIXES):
            m = _IMAGE.match(n)
            return str(dirpath / n), (m.group("w") if m else "")
    return None


def _walk(base: Path, layout: str) -> dict[tuple[str, str], tuple[str, str]]:
    """(bench, cp) -> (image, weight string) for every image found under `base`."""
    found: dict[tuple[str, str], tuple[str, str]] = {}
    try:
        tops = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError:
        return found
    for top in tops:
        if not top.is_dir():
            continue
        parts = top.name.rsplit("_", 2)
        if layout == "take_cpt" and len(parts) == 3 and (Path(top.path) / "0").is_dir():
            bench, cp, w = parts                            # take_cpt/<bench>_<cp>_<w>/0/
            hit = _image_in(Path(top.path) / "0", cp)
            if hit:
                found[(bench, cp)] = (hit[0], w)
            continue
        try:                                                # <base>/<bench>/<cp>/
            cps = sorted(os.scandir(top.path), key=lambda e: e.name)
        except OSError:
            continue
        for c in cps:
            if c.is_dir():
                hit = _image_in(Path(c.path), c.name)
                if hit:
                    found[(top.name, c.name)] = hit
    return found


def _signature(d: Path, wjson: Path | None, base: Path | None) -> list:
    sig = []
    for p in (d, wjson, base):
        try:
            st = p.stat() if p else None
            sig.append([str(p), st.st_mtime_ns, st.st_size] if st else None)
        except OSError:
            sig.append([str(p), None, None])
    return [INDEX_VERSION, *sig]


def scan(d: Path) -> dict:
    """Full index of the set in `d` (points with images)."""
    d = Path(d)
    m = _SPEC.search(d.name)
    out = {"name": d.name, "path": str(d), "spec": m.group(1) if m else None,
           "label": f"spec{m.group(1)}" if m else None, "layout": None, "multihart": _multihart(d),
           "json": None, "json_sha256": None, "has_insts": False, "insts": {}, "points": {},
           "benches": 0, "missing": 0, "extra": 0, "restorer_needed": False, "error": ""}
    layout, base, err = _layout(d)
    out["layout"], out["error"] = layout, err
    if not layout:
        return out
    wjson = _weights_json(d)
    weights: dict[str, dict[str, str]] = {}
    if wjson:
        raw = wjson.read_bytes()
        out["json"], out["json_sha256"] = str(wjson), hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw)
        except ValueError as error:
            out["error"] = f"{wjson}: {error}"
            return out
        for bench, info in (data or {}).items():
            if isinstance(info, dict) and isinstance(info.get("points"), dict):
                weights[bench] = {str(k): str(v) for k, v in info["points"].items()}
                if "insts" in info:
                    out["insts"][bench] = str(info["insts"])
            elif isinstance(info, dict):
                weights[bench] = {str(k): str(v) for k, v in info.items()}
    out["has_insts"] = bool(out["insts"])
    images = _walk(base, layout)
    points: dict[str, dict] = {}
    if weights:
        for bench, cps in weights.items():
            for cp, w in cps.items():
                hit = images.pop((bench, cp), None)
                if not hit:
                    out["missing"] += 1
                    continue
                name = f"{bench}_{cp}_{w6(hit[1] or w)}"
                points[name] = {"bench": bench, "cp": cp, "weight": w, "image": hit[0]}
        out["extra"] = len(images)
    else:
        for (bench, cp), (img, w) in images.items():
            if not w:
                out["missing"] += 1
                continue
            points[f"{bench}_{cp}_{w6(w)}"] = {"bench": bench, "cp": cp, "weight": w, "image": img}
    out["points"] = dict(sorted(points.items()))
    out["benches"] = len({p["bench"] for p in points.values()})
    out["restorer_needed"] = any(p["image"].endswith(".gz") for p in points.values())
    if not points and not out["error"]:
        out["error"] = "no checkpoint images found"
    return out


# --------------------------------------------------------------------- cache
def _cache_file(d: Path) -> Path:
    return cache_dir() / f"{d.name}-{hashlib.sha1(str(d).encode()).hexdigest()[:8]}.json"


_memo: dict[str, tuple[list, dict]] = {}


def index(d: Path, refresh: bool = False) -> dict:
    d = Path(d)
    layout, base, _ = _layout(d)
    sig = _signature(d, _weights_json(d) if layout else None, base)
    cf = _cache_file(d)
    if not refresh:
        hit = _memo.get(str(cf))
        if hit and hit[0] == sig:
            return hit[1]
        hit = store.read_json(cf)
        if isinstance(hit, dict) and hit.get("sig") == sig:
            _memo[str(cf)] = (sig, hit["index"])
            return hit["index"]
    idx = scan(d)
    _memo[str(cf)] = (sig, idx)
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        store.atomic_write_text(cf, json.dumps({"sig": sig, "index": idx}))
    except OSError:
        pass
    return idx


def summary(idx: dict) -> dict:
    s = {k: v for k, v in idx.items() if k not in ("points", "insts")}
    s["n_points"] = len(idx.get("points") or {})
    s["runnable"] = not idx.get("error") and not idx.get("multihart") and s["n_points"] > 0
    s["traced_by"] = [w for w, wl in (config.settings().get("workloads") or {}).items()
                      if (wl or {}).get("checkpoint") == idx["name"]]
    return s


def catalog(refresh: bool = False, spec: str | None = None) -> list[dict]:
    dirs = [d for n, d in set_dirs().items() if not spec or (_SPEC.search(n) and _SPEC.search(n).group(1) == spec)]
    if not dirs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(dirs))) as pool:
        idxs = list(pool.map(lambda d: index(d, refresh), dirs))
    return [summary(i) for i in idxs]


def find(ref: str) -> Path:
    """A set name (from the roots) or a set dir path."""
    if not ref:
        raise CkptError("empty checkpoint set name")
    if "/" in ref:
        p = Path(ref)
        if not p.is_dir():
            raise CkptError(f"checkpoint set {ref}: no such directory")
        return p
    d = set_dirs().get(ref)
    if not d:
        known = sorted(set_dirs())
        near = [n for n in known if ref in n or n in ref][:6]
        raise CkptError(f"unknown checkpoint set {ref!r} (featx ckpt list; checkpoints.roots: "
                        f"{', '.join(map(str, roots())) or 'none'})" + (f"; similar: {', '.join(near)}" if near else ""))
    return d


def get(ref: str, refresh: bool = False) -> dict:
    return index(find(ref), refresh)


def runnable(ref: str) -> dict:
    """The index of a set that exec runs can use, else CkptError."""
    idx = get(ref)
    if idx.get("multihart"):
        raise CkptError(f"checkpoint set {idx['name']} is multi-hart; featx runs single-core emus only")
    if idx.get("error"):
        raise CkptError(f"checkpoint set {idx['name']}: {idx['error']}")
    return idx


def provenance(idx: dict) -> dict:
    """What a runset records about its set (experiment.yaml run.checkpoint_set)."""
    return {"name": idx["name"], "path": idx["path"], "layout": idx["layout"], "json": idx["json"],
            "json_sha256": idx["json_sha256"], "points": len(idx["points"])}


def insts_path(name: str | None) -> str:
    """The set's {bench: {insts, points}} json, else the insts of a workload traced from it."""
    if not name:
        return ""
    try:
        idx = get(name)
    except CkptError:
        idx = None
    if idx and idx.get("has_insts") and idx.get("json"):
        return idx["json"]
    from . import presets
    for wname, wl in presets.workloads().items():
        if (wl or {}).get("checkpoint") == name:
            return presets.insts(wname)
    return ""
