"""Portability: config layering (package -> site -> user -> env), ${share}, the NEMU .so of
git.repo, site detection, featx init / doctor, the root pointer, site baselines, plugin
requirements, the shipped analysis scripts, and package resources without REPO_DIR."""

import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from featx import baselines, bootstrap, cli, config, doctor, recipes, service
from featx.post import plugins
from featx.sched.scheduler import SchedError
from featx.tree import Tree

from conftest import update_layer
from test_sched_local import sched


def _site(tmp_path, **data):
    p = tmp_path / "site.yaml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


def test_layers_package_site_user_env(root, tmp_path, monkeypatch):
    s = config.settings()
    assert s["site"] is None and s["git"]["repo"] is None and s["paths"]["dramsim3"] is None
    assert s["wt_root"] == str(tmp_path / "wt")                   # FEATX_WT_ROOT
    site = _site(tmp_path, hosts=["h1", "h2"], paths={"gsim": "/site/gsim", "dramsim3": "/site/dram"},
                 workloads={"w": {"label": "W"}}, placement={"reserve_cores": 9},
                 analysis_dir="${home}/tools")
    update_layer(root, site=site, paths={"dramsim3": "/mine/dram"})
    s = config.settings()
    assert s["site"] == site and s["hosts"] == ["localhost"]       # the user layer wins
    assert s["paths"]["gsim"] == "/site/gsim" and s["paths"]["dramsim3"] == "/mine/dram"
    assert s["placement"]["reserve_cores"] == 9 and s["placement"]["probe_ttl"] == 60
    assert s["workloads"] == {"w": {"label": "W"}}
    assert s["analysis_dir"] == str(Path.home() / "tools")         # ${home} expanded
    # the env picks another site over the user layer's
    other = tmp_path / "other.yaml"
    other.write_text("paths: {gsim: /other/gsim}\n")
    monkeypatch.setenv("FEATX_SITE", str(other))
    config.reload_settings()
    assert config.settings()["paths"]["gsim"] == "/other/gsim"
    monkeypatch.setenv("FEATX_SITE", "nosuch")
    config.reload_settings()
    with pytest.raises(config.ConfigError, match="site profile 'nosuch' not found"):
        config.settings()


def test_share_moves_every_site_path(root, tmp_path, monkeypatch):
    site = _site(tmp_path, share="/mnt/a", paths={"gsim": "${share}/bin/gsim"},
                 checkpoints={"roots": ["${share}/ck"]}, baselines={"b": {"root": "${share}/b"}})
    update_layer(root, site=site)
    s = config.settings()
    assert s["paths"]["gsim"] == "/mnt/a/bin/gsim" and s["checkpoints"]["roots"] == ["/mnt/a/ck"]
    assert baselines.load()["b"]["root"] == "/mnt/a/b"
    update_layer(root, share="/mnt/b/")
    assert config.settings()["paths"]["gsim"] == "/mnt/b/bin/gsim"
    # The committed profile prefers /nfs/share independently of this test host.
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: True if str(p).startswith("/nfs/share/")
                        else original_exists(p))
    update_layer(root, site="nfs-share", share=None)
    s = config.settings()
    assert s["share"] == "/nfs/share" and s["paths"]["gsim"] == "/nfs/share/bin/gsim"
    assert all(r.startswith("/nfs/share/") for r in s["checkpoints"]["roots"])
    assert "${share}" not in repr(s)
    update_layer(root, share="/nfs/share")
    assert config.settings()["workloads"]["spec06"]["manifest"].startswith("/nfs/share/gkc/traces/")


def test_shared_path_fallback(root, tmp_path):
    primary, fallback = tmp_path / "primary", tmp_path / "fallback"
    primary.mkdir()
    fallback.mkdir()
    (fallback / "tool").touch()
    (fallback / "points").mkdir()
    site = _site(tmp_path, share=str(primary), share_fallbacks=[str(fallback)],
                 paths={"gsim": "${share}/tool"},
                 recipe_defaults={"run": {"checkpoint_file": "${share}/points/${point.cp}.gz"}})
    update_layer(root, site=site)
    assert config.settings()["paths"]["gsim"] == str(fallback / "tool")
    assert config.settings()["recipe_defaults"]["run"]["checkpoint_file"].startswith(str(fallback))
    (primary / "tool").touch()
    config.reload_settings()
    assert config.settings()["paths"]["gsim"] == str(primary / "tool")
    (primary / "tool").unlink()
    update_layer(root, share_fallbacks=[])
    assert config.settings()["paths"]["gsim"] == str(primary / "tool")


def test_site_detection_uses_shared_fallback(tmp_path, monkeypatch):
    sites = tmp_path / "sites"
    sites.mkdir()
    fallback = tmp_path / "fallback"
    (fallback / "checkpoints").mkdir(parents=True)
    (sites / "cluster.yaml").write_text(yaml.safe_dump({
        "share": str(tmp_path / "missing"), "share_fallbacks": [str(fallback)],
        "detect": ["${share}/checkpoints"],
    }))
    monkeypatch.setattr(config, "SITES_DIR", sites)
    assert config.detect_site() == "cluster"


def test_nemu_diff_defaults_to_the_repo_ready_to_run(root, tmp_path):
    repo = tmp_path / "xs"
    update_layer(root, git={"repo": str(repo)})
    assert config.settings()["paths"]["nemu_diff"] is None              # no ready-to-run .so: --no-diff
    so = repo / "ready-to-run" / "riscv64-nemu-interpreter-so"
    so.parent.mkdir(parents=True)
    so.write_text("so")
    config.reload_settings()
    assert config.settings()["paths"]["nemu_diff"] == str(so) and recipes.path_vars()["paths.nemu_diff"] == str(so)
    update_layer(root, paths={"nemu_diff": "/other.so"})
    assert config.settings()["paths"]["nemu_diff"] == "/other.so"
    update_layer(root, paths={"nemu_diff": ""})                         # explicitly off
    assert recipes.path_vars()["paths.nemu_diff"] == ""


def test_detect_site(tmp_path, monkeypatch):
    sites = tmp_path / "sites"
    sites.mkdir()
    (sites / "a.yaml").write_text(yaml.safe_dump({"detect": [str(tmp_path / "nope")], "hosts": ["x"]}))
    (sites / "b.yaml").write_text(yaml.safe_dump({"detect": [str(tmp_path)], "hosts": ["y"]}))
    (sites / "c.yaml").write_text(yaml.safe_dump({"hosts": ["z"]}))   # no detect: never picked
    monkeypatch.setattr(config, "SITES_DIR", sites)
    assert config.detect_site() == "b" and "detect" not in config.site_layer("b")
    (sites / "b.yaml").unlink()
    assert config.detect_site() is None


def test_user_layer_validation_knows_the_site(root, tmp_path):
    with pytest.raises(ValueError, match="site profile 'nope' not found|site: unknown site"):
        config.write_user_layer("site: nope\n")
    config.write_user_layer("site: nfs-share\nworkloads: {mine: {label: x}}\n")
    assert "mine" in config.settings()["workloads"] and "spec06" in config.settings()["workloads"]
    r = config.set_user_key("recipe_defaults.run.W", 5)
    assert r["changed"] == ["recipe_defaults"]


def test_require_and_path_vars(root):
    with pytest.raises(config.ConfigError, match="git.repo is not set"):
        config.require("git.repo")
    v = recipes.path_vars()
    assert v["paths.gsim"] == "gsim" and v["paths.dramsim3"] == "" and v["paths.xiangshan"] == ""
    update_layer(root, git={"repo": "/r"})
    assert recipes.path_vars()["paths.xiangshan"] == "/r"


def test_root_pointer(tmp_path, monkeypatch):
    monkeypatch.delenv("FEATX_ROOT", raising=False)
    monkeypatch.setenv("FEATX_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config._pointed_root.cache_clear()
    assert config.root() == tmp_path / "data" / "featx"
    config.set_root_pointer(tmp_path / "elsewhere")
    assert config.root() == tmp_path / "elsewhere"
    assert config.token_path() == tmp_path / "cfg" / "token" or os.environ.get("FEATX_TOKEN_FILE")
    config._pointed_root.cache_clear()
    config.reload_settings()


def test_init_new_and_adopt(tmp_path, monkeypatch):
    monkeypatch.delenv("FEATX_ROOT", raising=False)
    monkeypatch.delenv("DRAMSIM3_HOME", raising=False)
    monkeypatch.setenv("FEATX_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("FEATX_TOKEN_FILE", str(tmp_path / "cfg" / "token"))
    repo = tmp_path / "xs"
    (repo / ".git").mkdir(parents=True)
    target = tmp_path / "root"
    try:
        r = bootstrap.init(str(target), site="nfs-share", repo=str(repo), extra={"hosts": ["localhost"]})
        layer = yaml.safe_load((target / "featx.yaml").read_text())
        assert layer["site"] == "nfs-share" and layer["git"]["repo"] == str(repo)
        assert layer["daemon"]["port"] == bootstrap.default_port() and layer["hosts"] == ["localhost"]
        assert (tmp_path / "cfg" / "root").read_text().strip() == str(target) and config.root() == target
        assert (tmp_path / "cfg" / "token").is_file() and (target / "tree").is_dir() and not r["adopted"]
        with pytest.raises(config.ConfigError, match="pass --adopt"):
            bootstrap.init(str(target))
        # adopting keeps the existing keys (and port) and adds the given ones
        r = bootstrap.init(str(target), daemon_host="hostx", adopt=True, extra={"paths": {"dramsim3": "/d"}})
        layer = yaml.safe_load((target / "featx.yaml").read_text())
        assert r["adopted"] and layer["daemon"] == {"port": bootstrap.default_port(), "host": "hostx"}
        assert layer["paths"] == {"dramsim3": "/d"} and layer["git"]["repo"] == str(repo)
        with pytest.raises(config.ConfigError, match="not found"):
            bootstrap.init(str(tmp_path / "r2"), site="nosuch")
        with pytest.raises(config.ConfigError, match="not a git checkout"):
            bootstrap.init(str(tmp_path / "r2"), repo=str(tmp_path))
        monkeypatch.setattr(config, "detect_site", lambda: None)
        out = CliRunner().invoke(cli.app, ["init", "--root", str(tmp_path / "r3"), "--port", "9001"])
        assert out.exit_code == 0, out.output
        assert "featx daemon start" in out.output and ":9001/ui/" in out.output
        assert "site" not in yaml.safe_load((tmp_path / "r3" / "featx.yaml").read_text())
        # without --site the profile detected on this host is taken; --site none opts out
        monkeypatch.setattr(config, "detect_site", lambda: "nfs-share")
        out = CliRunner().invoke(cli.app, ["init", "--root", str(tmp_path / "r4")])
        assert "detected on this host" in out.output
        assert yaml.safe_load((tmp_path / "r4" / "featx.yaml").read_text())["site"] == "nfs-share"
        r = bootstrap.init(str(tmp_path / "r5"), site="none")
        assert not r["site_detected"] and "site" not in yaml.safe_load((tmp_path / "r5" / "featx.yaml").read_text())
        r = bootstrap.init(str(target), adopt=True)         # a layer with a site keeps it
        assert not r["site_detected"] and "site" not in r["written"]
    finally:
        config._pointed_root.cache_clear()
        config.reload_settings()


def test_doctor_local(root, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "detect_site", lambda: None)
    r = doctor.run(ssh=False)
    by = {c["check"]: c for c in r["checks"]}
    assert by["data root"]["status"] == "ok" and by["site profile"]["status"] == "warn"
    assert "workloads, checkpoints and baselines are all unset" in by["site profile"]["detail"]
    assert by["git.repo"]["status"] == "warn" and by["analysis scripts"]["status"] == "ok"
    assert "shipped with featx" in by["analysis scripts"]["detail"]
    assert r["ok"]
    monkeypatch.setattr(config, "detect_site", lambda: "nfs-share")
    by = {c["check"]: c for c in doctor.run(ssh=False)["checks"]}
    assert "featx config set site=nfs-share" in by["site profile"]["detail"]
    update_layer(root, analysis_dir=str(tmp_path / "noanalysis"))
    assert {c["check"]: c for c in doctor.run(ssh=False)["checks"]}["analysis scripts"]["status"] == "fail"
    update_layer(root, analysis_dir=None)
    update_layer(root, paths={"gsim": str(tmp_path / "nogsim")},
                 workloads={"w": {"manifest": str(tmp_path / "none.tsv")}})
    by = {c["check"]: c for c in doctor.run(ssh=False)["checks"]}
    assert by["paths.gsim"]["status"] == "warn" and "not an executable" in by["paths.gsim"]["detail"]
    assert by["workload w: manifest"]["status"] == "warn"
    assert doctor.host_checks(["localhost"]) == [{"check": "host localhost", "status": "ok", "detail": "local"}]
    out = CliRunner().invoke(cli.app, ["doctor", "--no-ssh"])
    assert out.exit_code == 0 and "data root" in out.output


def test_site_baselines_overlay(root, tmp_path):
    site = _site(tmp_path, baselines={"ref": {"root": "/shared/ref", "description": "d", "checkpoint": "ck"}})
    update_layer(root, site=site)
    assert baselines.load() == {"ref": {"root": "/shared/ref", "description": "d", "checkpoint": "ck"}}
    assert not baselines.path().exists()                  # nothing is copied into the root
    baselines.add("ref", "/shared/ref", "d", collected="/c")   # per-entry overlay keeps the site's fields
    assert baselines.load()["ref"] == {"root": "/shared/ref", "description": "d", "checkpoint": "ck", "collected": "/c"}
    baselines.add("mine", "/m", "x")
    service.baseline_rm("ref", force=True)                # a site entry is hidden, not deleted
    assert set(baselines.load()) == {"mine"}
    assert yaml.safe_load(baselines.path().read_text())["ref"] is None


def test_plugin_requires_refuses_submit(root, fake_emu):
    from test_sched_local import make_node
    (root / "plugins").mkdir()
    (root / "plugins" / "needs-pt.yaml").write_text(yaml.safe_dump(
        {"name": "needs-pt", "requires": ["paths.perf_trigger"], "cmd": "true"}))
    make_node(fake_emu, ["ok_1_0.5"], steps={"c": {"plugin": "needs-pt"}})
    assert plugins.missing_requirements(plugins.get("needs-pt")) == ["paths.perf_trigger"]
    with pytest.raises(SchedError, match="needs paths.perf_trigger"):
        sched().submit("t/sched/x", ["run", "post"], bin_ref=str(fake_emu[0]))
    out = CliRunner().invoke(cli.app, ["plugin", "list"])
    assert "[off: set paths.perf_trigger]" in out.output


def test_analysis_scripts_ship_with_featx(root, tmp_path):
    """collect / pair / diagnose / branch-mpki run the packaged scripts unless analysis_dir is set."""
    for m in ("collect", "pair", "diagnose", "branch-mpki"):
        assert not plugins.missing_requirements(plugins.get(m)), m
        script = plugins.get(m)["cmd"].split("${tools}/")[1].split()[0]
        assert (config.ANALYSIS_DIR / script).is_file(), script
    entry = Tree().add("t/a", parents=True)
    assert recipes.base_context(Tree(), entry)["tools"] == str(config.ANALYSIS_DIR)
    update_layer(root, analysis_dir=str(tmp_path / "mine"))
    assert recipes.base_context(Tree(), entry)["tools"] == str(tmp_path / "mine")


def test_trace_root_unset_is_refused(root, fake_emu):
    Tree().add("t/n", parents=True)
    service.recipe_set("t/n", "run", {"points": ["ok_1_0.5"]})
    with pytest.raises(SchedError, match="trace_root is unset"):
        sched().submit("t/n", ["run"], bin_ref=str(fake_emu[0]))


def test_resources_live_in_the_package():
    for p in (config.BIN_DIR / "featx-exec", config.BIN_DIR / "runwatch.sh", config.BIN_DIR / "add-worktree.sh",
              config.DEFAULTS_DIR / "classify.yaml", config.DOCS_DIR / "reference.md",
              config.SITES_DIR / "nfs-share.yaml"):
        assert p.is_file() and p.resolve().is_relative_to(config.PKG_DIR)
    for p in config.BIN_DIR.iterdir():
        assert os.access(p, os.X_OK), p


def test_package_has_no_personal_paths():
    """Nothing names a user's home; only the site profile names cluster hosts."""
    hits = []
    for p in config.PKG_DIR.rglob("*"):
        if p.is_dir() or "web" in p.relative_to(config.PKG_DIR).parts[:1] or p.suffix in (".pyc",):
            continue
        text = p.read_text(errors="ignore")
        for word in ("gongkaichen", "node024"):
            if word in text and not (word == "node024" and p.parent.name == "sites"):
                hits.append(f"{p.relative_to(config.PKG_DIR)}: {word}")
    assert not hits, hits
