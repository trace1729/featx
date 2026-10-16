"""docs/reference.md as help topics (CLI, REST, MCP), plugin list/show, baseline rm, bin --mode."""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from featx import baselines, cli, docs, provenance, service, store
from featx.sched import daemon
from featx.tree import Tree, TreeError

TOPICS = ("concepts", "launch", "recipes", "build", "run", "points", "workloads", "post",
          "variables", "references", "plugins", "worktrees", "baselines", "checkpoints")


def test_reference_plugin_section_is_current():
    src = docs.text()
    assert docs.splice(src, docs.plugin_markdown(docs.builtin_plugins())) == src, \
        "docs/reference.md is stale: run python -m featx.docs"


def test_topics_and_every_documented_key():
    found = docs.topics()
    assert tuple(found) == TOPICS
    # every key of the package recipe defaults appears in its topic
    from featx import config, store
    for kind in ("build", "run"):
        data = store.read_yaml(config.DEFAULTS_DIR / "recipes" / f"{kind}.yaml")
        body = found[kind][1]
        missing = [k for k in data if f"`{k}" not in body and f"{k}`" not in body]
        assert not missing, (kind, missing)
    # every plugin parameter has a meaning
    for name, man in docs.builtin_plugins().items():
        assert set(man.get("with") or {}) <= set(man.get("params") or {}), name
        assert "summary.json" in (man.get("outputs") or {}) or "suite.json" in (man.get("outputs") or {}), name


def test_topic_lookup(root):
    assert docs.topic("points").startswith("## points")
    assert "### perf-keys" in docs.topic("plugins") and docs.BEGIN not in docs.topic("plugins")
    assert docs.topic("pair").startswith("## pair") and "`reference`" in docs.topic("pair")
    assert "## worktrees" in docs.topic("all") and docs.END not in docs.topic("all")
    with pytest.raises(KeyError, match="no help topic"):
        docs.topic("nope")
    # a plugin under $FEATX_ROOT/plugins shows up live
    (root / "plugins").mkdir()
    (root / "plugins" / "mine.yaml").write_text("name: mine\ndescription: my step\nwith: {x: 1}\n"
                                                "params: {x: the x}\ncmd: echo\n")
    assert "### mine" in docs.topic("plugins") and "my step" in docs.topic("mine")


def test_cli_help_and_plugins(root):
    run = CliRunner().invoke
    r = run(cli.app, ["help"])
    assert r.exit_code == 0 and all(t in r.output for t in TOPICS)
    r = run(cli.app, ["help", "variables"])
    assert r.exit_code == 0 and "${point.checkpoint}" in r.output
    r = run(cli.app, ["help", "nope"])
    assert r.exit_code == 1 and "no help topic" in r.output
    r = run(cli.app, ["plugin", "list"])
    assert r.exit_code == 0 and "perf-keys" in r.output and "collect" in r.output
    r = run(cli.app, ["plugin", "show", "collect"])
    assert r.exit_code == 0 and "| `N` | `2` |" in r.output
    r = run(cli.app, ["plugin", "show", "collect", "--raw"])
    assert r.exit_code == 0 and "cmd:" in r.output
    assert run(cli.app, ["plugin", "show", "nope"]).exit_code == 1


def test_baseline_rm_refuses_users(nfs_site, tmp_path):
    res = tmp_path / "res"
    res.mkdir()
    service.baseline_add("mine", str(res), "x")
    Tree().add("a/b", parents=True)
    service.recipe_set("a/b", "post", {"steps": {"pk": {"plugin": "perf-keys", "baselines": ["mine"]}}})
    with pytest.raises(TreeError, match="used by a/b"):
        service.baseline_rm("mine")
    with pytest.raises(TreeError, match="used by workload:spec06"):
        service.baseline_rm("exec-0903")
    with pytest.raises(TreeError, match="unknown baseline"):
        service.baseline_rm("nope")
    # a name that is only a substring of a recipe word is not a use
    service.recipe_set("a/b", "post", {"steps": {"pk": {"plugin": "perf-keys", "baselines": ["mine2"]}}},
                       merge=False)
    assert service.baseline_rm("mine") == {"removed": "mine", "used_by": []}
    assert "mine" not in baselines.load()
    r = CliRunner().invoke(cli.app, ["baseline", "rm", "exec-0903", "--force"])
    assert r.exit_code == 0 and json.loads(r.output)["used_by"] == ["workload:spec06"]


def test_bin_import_mode(root, tmp_path):
    emu = tmp_path / "emu"
    emu.write_text("#!/bin/sh\n")
    key = service.bin_import(str(emu), "x")["key"]
    manifest = lambda: store.read_json(provenance.bincache_entry(key) / "manifest.json")
    assert "mode" not in manifest()
    assert service.bin_import(str(emu), "x", "exec")["key"] == key
    assert manifest()["mode"] == "exec"
    with pytest.raises(TreeError, match="trace\\|exec"):
        service.bin_import(str(emu), "x", "rtl")


def test_rest_docs_and_baseline_delete(nfs_site, tmp_path):
    d = daemon.Daemon(port=1)
    c = TestClient(daemon.build_app(d))
    h = {"X-Featx-Token": d.token}
    idx = c.get("/help", headers=h).json()
    assert [t["topic"] for t in idx["topics"]] == list(TOPICS)
    assert c.get("/help/run", headers=h).json()["text"].startswith("## run")
    assert c.get("/help/nope", headers=h).status_code == 400
    assert "perf-keys" in c.get("/plugins", headers=h).json()
    assert c.delete("/baselines/exec-0903", headers=h).status_code == 400
    assert c.delete("/baselines/exec-0903", params={"force": True}, headers=h).json()["removed"] == "exec-0903"


def test_mcp_docs_tool(root):
    from featx import mcp_server
    assert [t["topic"] for t in mcp_server.docs()["topics"]] == list(TOPICS)
    assert mcp_server.docs("references").startswith("## references")
