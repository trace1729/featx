from copy import deepcopy

import pytest
from typer.testing import CliRunner

from featx.cli import app
from featx.waiting import wait_tasks


class Pipeline:
    def __init__(self, state="running", change=None):
        self.tasks = {
            "build": {"id": "build", "state": state, "after": []},
            "run": {"id": "run", "state": "queued", "after": ["build"]},
            "post": {"id": "post", "state": "queued", "after": ["run"]},
        }
        self.change = change
        self.waits = []

    def get(self, path, **params):
        if path.startswith("/tasks/"):
            return deepcopy(self.tasks[path.rsplit("/", 1)[1]])
        assert path == "/events"
        assert params["unacked"] is False
        self.waits.append(params)
        assert len(self.waits) == 1, "unexpected polling after dependency failure"
        self.tasks["build"]["state"] = self.change
        return [{"seq": 1, "type": "task_done", "task": "build"}]


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_existing_failure_stops_transitive_wait(state):
    c = Pipeline(state)
    result = wait_tasks(c, ["build", "run", "post"])
    assert result["finished"]["build"]["state"] == state
    assert set(result["blocked"]) == {"run", "post"}
    assert result["blocked"]["post"]["failed_dependencies"] == ["build"]
    assert not result["pending"] and not result["timed_out"]
    assert not c.waits
    assert c.tasks["post"]["state"] == "queued"


def test_post_only_wait_wakes_on_upstream_failure():
    c = Pipeline(change="failed")
    result = wait_tasks(c, ["post"])
    assert result["blocked"]["post"]["failed_dependencies"] == ["build"]
    assert set(c.waits[0]["task"].split(",")) == {"build", "run", "post"}
    assert not result["timed_out"]


def test_zero_timeout_checks_once():
    c = Pipeline()
    result = wait_tasks(c, ["post"], timeout=0)
    assert result["pending"] == ["post"] and result["timed_out"]
    assert not c.waits


def test_already_successful():
    c = Pipeline("succeeded")
    result = wait_tasks(c, ["build"])
    assert result["finished"]["build"]["state"] == "succeeded"
    assert not result["blocked"] and not result["timed_out"]


def test_cli_blocked_exit(monkeypatch):
    monkeypatch.setattr("featx.cli.client", lambda: Pipeline("failed"))
    result = CliRunner().invoke(app, ["wait", "post", "--json"])
    assert result.exit_code == 1
    assert '"failed_dependencies"' in result.output
