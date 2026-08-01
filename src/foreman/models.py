"""Data model for the pipeline.

Everything crossing a layer boundary is one of these dataclasses. The
orchestration loop only ever sees these types, never raw JSON, subprocess
output, or SDK message objects. That is what lets the whole pipeline be tested
without a network, a repo, or an API key: a fake only has to return one of
these, not impersonate git or an SDK.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def iso_now() -> str:
    """UTC timestamp in ISO 8601, used for every recorded time."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class SubTaskStatus(str, Enum):
    """Sub-task lifecycle. `blocked` requires a blocked_reason."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"


class TicketStatus(str, Enum):
    """Ticket lifecycle. `in_review` is terminal pre-gate: the pipeline stops
    there and a human decides what happens next."""

    PULLED = "pulled"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"
    FAILED = "failed"
    DONE = "done"


class Priority(str, Enum):
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Ticket:
    """A Linear issue. This is the only unit Linear ever sees."""

    identifier: str  # e.g. "TEAM-42", whatever your Linear team key is
    title: str
    description: str = ""
    status: str = "todo"  # raw Linear state name, lowercased
    priority: str = Priority.MEDIUM.value
    labels: list[str] = field(default_factory=list)
    project: str | None = None
    estimate: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    url: str | None = None

    @property
    def prefix(self) -> str:
        """The team prefix, e.g. 'TEAM' from 'TEAM-42'. Never hardcoded anywhere."""
        return self.identifier.rsplit("-", 1)[0]

    def is_done(self) -> bool:
        return self.status.lower() in {"done", "completed", "merged", "closed"}


@dataclass
class SubTask:
    """A local decomposition unit. Never pushed to Linear."""

    id: str  # "<TICKET>.NN"
    goal: str
    status: str = SubTaskStatus.PENDING.value
    depends_on: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    log: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # Recorded from the SpawnResult of the session that ran this sub-task.
    # Both stay None until a session finishes, and for sub-tasks written by a
    # Foreman old enough not to have captured them.
    session_id: str | None = None
    cost_usd: float | None = None

    def is_done(self) -> bool:
        return self.status == SubTaskStatus.DONE.value


@dataclass
class TicketState:
    """Contents of `.foreman/<TICKET>/state.json`. The source of truth.

    `manifest.md` is rendered from this on every write and is never parsed back.
    """

    ticket: str
    title: str = ""
    status: str = TicketStatus.PULLED.value
    branch: str = ""
    pulled_at: str = ""
    pr_url: str | None = None
    subtasks: list[SubTask] = field(default_factory=list)

    def subtask(self, subtask_id: str) -> SubTask:
        for st in self.subtasks:
            if st.id == subtask_id:
                return st
        raise KeyError(f"no such sub-task: {subtask_id}")

    def done_ids(self) -> set[str]:
        return {st.id for st in self.subtasks if st.is_done()}

    def all_done(self) -> bool:
        return bool(self.subtasks) and all(st.is_done() for st in self.subtasks)

    def unfinished(self) -> list[SubTask]:
        return [st for st in self.subtasks if not st.is_done()]


@dataclass
class SpawnResult:
    """Structured return of one agent session.

    The `done` / `blocked` / `failed` split is load bearing:

      done    - the sub-task is complete.
      blocked - a clean run where the agent reported an EXTERNAL blocker it
                cannot resolve. A normal business outcome, not an error, and
                never retried: nothing we do will change the answer.
      failed  - an infra or SDK error, a turn-limit hit, or unparseable output.
                Often transient, so this is the one the orchestrator retries.
    """

    status: str  # "done" | "blocked" | "failed"
    summary: str = ""
    blocked_reason: str | None = None
    error: str | None = None
    session_id: str | None = None
    total_cost_usd: float | None = None
    raw: str | None = None
    # False when the failure will still be there in ten seconds. A usage limit
    # or a bad credential is not transient the way a dropped connection is, so
    # retrying it immediately just burns the second attempt for nothing.
    retryable: bool = True

    @property
    def ok(self) -> bool:
        return self.status == SubTaskStatus.DONE.value


@dataclass
class SubTaskSpec:
    """What a decomposition emits, before it becomes a SubTask on disk."""

    goal: str
    depends_on: list[str] = field(default_factory=list)
    definition_of_done: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    test_plan: str = ""
    notes: str = ""


@dataclass
class CommitResult:
    created: bool
    sha: str | None = None
    message: str = ""


@dataclass
class PullRequest:
    url: str | None
    branch: str
    title: str
    body: str
    manual: bool = False  # True when gh was unavailable and the human must open it


@dataclass
class RunReport:
    """What one `foreman pull` produced. Returned by the orchestrator so the CLI
    can print it without the loop doing any I/O of its own."""

    ticket: str | None = None
    outcome: str = "no_work"  # no_work | in_review | halted | error
    branch: str | None = None
    pr_url: str | None = None
    detail: str = ""
    notes: list[str] = field(default_factory=list)
    subtasks: list[dict[str, Any]] = field(default_factory=list)
