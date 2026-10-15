"""Persistent records. Nodes live in tree/<path>/node.yaml, runsets in
tree/<path>/<YYYYMMDD-n>/experiment.yaml, tasks in tasks/<id>.json (daemon-owned)."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RUNSET_RE = re.compile(r"^\d{8}-\d+$")
RESERVED_NAMES = {"recipes", "scripts", "build", "run", "post", "tasks", "notes"}

KINDS = ("category", "feature", "subfeature")
VERDICTS = ("open", "supported", "refuted", "inconclusive")
NODE_STATUSES = ("active", "parked", "done", "abandoned")

TASK_KINDS = ("build", "run", "post")
ACTIVE_TASK_STATES = ("queued", "running")
TERMINAL_TASK_STATES = ("succeeded", "failed", "cancelled")
JOB_ACTIVE = ("queued", "launching", "running")

# node.yaml fields that node_update may set; git/moves/ids are owned by worktree/mv code
EDITABLE_FIELDS = ("hypothesis", "verdict", "verdict_note", "status", "notes", "comment",
                   "evidence", "tags")


def kind_for_depth(depth: int) -> str:
    return KINDS[min(depth, len(KINDS)) - 1]


class SubWorktree(BaseModel):
    repo: str                 # the submodule's main checkout (where `git worktree add` runs)
    base: str
    branch: str
    path: str
    managed: bool | None = None   # featx created the branch (None = recorded before the field)


class GitInfo(BaseModel):
    repo: str
    base: str
    branch: str
    worktree: str
    submodule_worktrees: dict[str, SubWorktree] = Field(default_factory=dict)
    managed: bool | None = None   # featx created the branch; adopted worktrees are False


class Node(BaseModel):
    id: str
    kind: str
    name: str
    parent_id: str | None = None
    hypothesis: str = ""
    verdict: str = "open"
    verdict_note: str = ""
    evidence: list[str] = Field(default_factory=list)   # runset refs backing the verdict
    status: str = "active"
    git: GitInfo | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    comment: str = ""         # free-form thoughts, shown under the evidence
    created: str = ""
    updated: str = ""
    moves: list[dict] = Field(default_factory=list)


class Runset(BaseModel):
    id: str
    name: str                 # YYYYMMDD-n
    node_id: str
    purpose: str = ""
    created: str = ""
    derived_from: str | None = None
    sealed: bool = False      # set once every task finished; further changes need a new runset
    provenance: dict[str, Any] = Field(default_factory=dict)
    recipes: dict[str, Any] = Field(default_factory=dict)   # snapshots taken at submit
    tasks: list[str] = Field(default_factory=list)
    bin: dict[str, Any] | None = None


class Job(BaseModel):
    name: str
    rel: str                  # job dir relative to the runset dir
    state: str = "queued"     # queued|launching|running|succeeded|failed|cancelled
    host: str | None = None
    attempt: int = 0
    cores: int = 1
    mem_gb: float = 4.0
    rc: int | None = None
    failure: dict | None = None
    launched: float | None = None
    started: float | None = None
    finished: float | None = None
    hb_value: str | None = None
    hb_seen: float | None = None
    progress: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)
    vars: dict[str, Any] = Field(default_factory=dict)
    local: bool = False       # must run on the daemon host (post-processing)


class Task(BaseModel):
    id: str
    kind: Literal["build", "run", "post"]
    name: str
    node_id: str
    runset_id: str
    runset_name: str
    after: list[str] = Field(default_factory=list)
    state: str = "queued"
    failure: dict | None = None
    spec: dict[str, Any] = Field(default_factory=dict)      # resolved recipe
    jobs: list[Job] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    created: str = ""
    started: float | None = None
    finished: float | None = None
    design_relaunches: int = 0
    max_design_relaunches: int = 3
    blocked: bool = False
    note: str = ""
    priority: int = 0         # higher launches first; FIFO by `created` within a priority

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for job in self.jobs:
            out[job.state] = out.get(job.state, 0) + 1
        return out
