"""The serial loop.

LOCKED: no parallelism, no locking. One sub-task at a time.
LOCKED: per-ticket human gate. The pipeline stops at PR-open plus
ticket-in-review. Nothing is ever merged or set to Done by this code.

This module performs no I/O of its own. Every external effect goes through a
port on `Deps`. That is what lets the entire pipeline be tested end to end
against fakes, with no network, no repository, and no model calls, which is the
only practical way to have confidence in a loop whose real runs cost money and
edit somebody's code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .models import (
    CommitResult,
    PullRequest,
    RunReport,
    SpawnResult,
    SubTask,
    SubTaskStatus,
    Ticket,
    TicketState,
    TicketStatus,
    iso_now,
)
from .state import StateStore, next_ready_subtask
from .topo import ready_nodes

_PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

# Total attempts per sub-task, so 2 means one retry. Only `failed` results are
# retried: a turn-limit hit or a dropped connection is usually transient, and
# halting a whole ticket over one is needless. `blocked` is never retried,
# because the answer will not change until a human does something.
SPAWN_ATTEMPTS = 2


# -- ports -------------------------------------------------------------------


class GitPort(Protocol):
    """Git and gh, already bound to one target repo."""

    def require_clean(self) -> None: ...
    def default_branch(self) -> str: ...
    def sync_default_branch(self) -> str: ...
    def branch_name(self, identifier: str, title: str) -> str: ...
    def create_branch(self, name: str) -> str: ...
    def commit(self, message: str) -> CommitResult: ...
    def push(self, branch: str) -> bool: ...
    def open_pull_request(
        self, branch: str, base: str, title: str, body: str
    ) -> PullRequest: ...


class SpawnPort(Protocol):
    """Run one sub-task in a fresh agent session.

    Keyword-only because the argument list grew: positional calls here are how
    a retry silently becomes a first attempt.
    """

    def __call__(
        self,
        *,
        ticket: Ticket,
        subtask: SubTask,
        readme: str,
        siblings: list[tuple[str, str]],
        attempt: int,
        previous_error: str | None,
    ) -> SpawnResult: ...


class LinearPort(Protocol):
    def list_assigned(self) -> list[Ticket]: ...
    def get(self, identifier: str) -> Ticket: ...
    def comment(self, identifier: str, body: str) -> None: ...
    def set_status(self, identifier: str, status: str) -> None: ...


@dataclass
class Deps:
    """Everything the loop is allowed to touch."""

    linear: LinearPort
    store: StateStore
    git: GitPort
    decompose: Callable[[Ticket], list[SubTask]]
    spawn: SpawnPort
    now: Callable[[], str] = iso_now
    attempts: int = SPAWN_ATTEMPTS


# -- ticket selection (pure) -------------------------------------------------


def select_ticket(tickets: list[Ticket]) -> Ticket | None:
    """Pick the next ticket to work.

    Only tickets whose every known blocker is done are eligible. A `blocked_by`
    pointing at something outside the assigned set is treated as satisfied: it
    is not ours to order. Tickets already sitting at `in_review` are skipped,
    because those are waiting on a human, not on us.

    Tie-break is a plain sort for v1: priority first, then how many other
    tickets this one unblocks, then identifier for determinism.
    """
    live = [t for t in tickets if not t.is_done() and t.status != TicketStatus.IN_REVIEW.value]
    if not live:
        return None

    by_id = {t.identifier: t for t in tickets}
    done = {t.identifier for t in tickets if t.is_done()}
    graph = {t.identifier: list(t.blocked_by) for t in live}

    ready = ready_nodes(graph, satisfied=done)
    if not ready:
        return None

    unblock_count: dict[str, int] = {tid: 0 for tid in ready}
    for t in tickets:
        for blocker in t.blocked_by:
            if blocker in unblock_count:
                unblock_count[blocker] += 1

    ready.sort(
        key=lambda tid: (
            _PRIORITY_RANK.get(by_id[tid].priority, 9),
            -unblock_count[tid],
            tid,
        )
    )
    return by_id[ready[0]]


# -- reporting (pure) --------------------------------------------------------


def pr_body(ticket: Ticket, state: TicketState) -> str:
    lines = [
        f"Closes {ticket.identifier}: {ticket.title}",
        "",
        ticket.description.strip() or "(no ticket description)",
        "",
        "## Sub-tasks completed",
        "",
    ]
    for st in state.subtasks:
        lines.append(f"### {st.id} {st.goal}")
        lines.append((st.log or "(no log recorded)").strip())
        lines.append("")
    lines += [
        "---",
        "",
        "Opened by Foreman. Not auto-merged: this PR and the ticket's in-review "
        "state are the human gate.",
    ]
    return "\n".join(lines)


def halt_comment(ticket: Ticket, state: TicketState) -> str:
    lines = [f"Foreman ran {ticket.identifier} on `{state.branch}` and stopped short.", ""]
    for st in state.subtasks:
        if st.status == SubTaskStatus.DONE.value:
            lines.append(f"- done: `{st.id}` {st.goal}")
    for st in state.subtasks:
        if st.status == SubTaskStatus.BLOCKED.value:
            lines.append(f"- blocked: `{st.id}` {st.goal} ({st.blocked_reason})")
        elif st.status == SubTaskStatus.FAILED.value:
            lines.append(f"- failed: `{st.id}` {st.goal} ({st.blocked_reason})")
        elif st.status == SubTaskStatus.PENDING.value:
            lines.append(f"- not started: `{st.id}` {st.goal}")
    lines += [
        "",
        "Resolve the blockers and run foreman again on this repo. The re-run "
        "skips sub-tasks that are already done.",
    ]
    return "\n".join(lines)


def _subtask_rows(state: TicketState) -> list[dict]:
    return [
        {
            "id": st.id,
            "goal": st.goal,
            "status": st.status,
            "reason": st.blocked_reason,
        }
        for st in state.subtasks
    ]


# -- the loop ----------------------------------------------------------------


def run_once(deps: Deps, ticket_id: str | None = None) -> RunReport:
    """Pull one ticket, take it as far as the gate, and stop.

    `ticket_id` forces a specific ticket instead of selecting one. That skips
    the readiness checks on purpose: naming a ticket explicitly is a deliberate
    act, and the usual reason to do it is to work something the selector would
    not have picked.

    Returns a report. Raises only for conditions that mean we should not have
    started at all, such as a dirty working tree.
    """
    if ticket_id:
        ticket = deps.linear.get(ticket_id)
        if ticket.is_done():
            return RunReport(
                ticket=ticket_id,
                outcome="no_work",
                detail=f"{ticket_id} is already finished",
            )
    else:
        ticket = select_ticket(deps.linear.list_assigned())
    if ticket is None:
        return RunReport(outcome="no_work", detail="no ready ticket assigned to me")

    state = _prepare(deps, ticket)
    _execute(deps, ticket, state)
    return _finalize(deps, ticket, state)


def _prepare(deps: Deps, ticket: Ticket) -> TicketState:
    """Get onto the ticket branch with a decomposed, seeded state.

    Resuming an existing ticket re-uses its branch and its state.json, which is
    how a re-run skips already-done sub-tasks.
    """
    deps.git.require_clean()

    if deps.store.exists(ticket.identifier):
        state = deps.store.load(ticket.identifier)
        deps.git.create_branch(state.branch)  # checks out the existing branch
        state.status = TicketStatus.IN_PROGRESS.value
        deps.store.save(state)
        return state

    deps.git.sync_default_branch()
    branch = deps.git.branch_name(ticket.identifier, ticket.title)
    deps.git.create_branch(branch)

    state = TicketState(
        ticket=ticket.identifier,
        title=ticket.title,
        status=TicketStatus.PULLED.value,
        branch=branch,
        pulled_at=deps.now(),
    )
    deps.store.save(state)

    state.subtasks = deps.decompose(ticket)
    state.status = TicketStatus.IN_PROGRESS.value
    deps.store.save(state)
    return state


def _execute(deps: Deps, ticket: Ticket, state: TicketState) -> None:
    """Run sub-tasks serially until nothing is runnable.

    The loop ends when no pending sub-task has all its dependencies done. With a
    correct topo-sort that is terminal for this run: a later sibling can never
    retroactively unblock an earlier one. Independent siblings still get their
    turn, which is why a blocked sub-task does not stop the loop outright. There
    is deliberately no retry pass and no max-iterations counter.
    """
    while True:
        subtask = next_ready_subtask(state)
        if subtask is None:
            return

        subtask.status = SubTaskStatus.IN_PROGRESS.value
        subtask.started_at = deps.now()
        deps.store.save(state)

        result = _spawn_with_retry(deps, ticket, subtask, state)

        subtask.finished_at = deps.now()
        if result.status == SubTaskStatus.DONE.value:
            subtask.status = SubTaskStatus.DONE.value
            subtask.log = result.summary
            deps.store.append_execution_log(
                ticket.identifier, subtask.id, f"[foreman] done: {result.summary}"
            )
            deps.store.save(state)
            # Commit per done sub-task so a mid-run failure leaves a clean,
            # inspectable tree instead of one undifferentiated blob.
            deps.git.commit(f"{ticket.identifier} {subtask.id}: {subtask.goal}")
        elif result.status == SubTaskStatus.BLOCKED.value:
            subtask.status = SubTaskStatus.BLOCKED.value
            subtask.blocked_reason = result.blocked_reason
            subtask.log = result.summary
            deps.store.append_execution_log(
                ticket.identifier, subtask.id, f"[foreman] blocked: {result.blocked_reason}"
            )
            deps.store.save(state)
        else:
            subtask.status = SubTaskStatus.FAILED.value
            subtask.blocked_reason = result.error or "agent failed"
            subtask.log = result.summary or None
            deps.store.append_execution_log(
                ticket.identifier, subtask.id, f"[foreman] failed: {subtask.blocked_reason}"
            )
            deps.store.save(state)


def _spawn_with_retry(
    deps: Deps, ticket: Ticket, subtask: SubTask, state: TicketState
) -> SpawnResult:
    """Run one sub-task, retrying only on `failed`.

    The working tree is deliberately NOT reset between attempts. A turn-limit
    failure usually means most of the work is already correct, so finishing it
    beats throwing it away; the retry is told it is a retry so it does not
    duplicate edits. Anything already committed belongs to a sub-task that
    finished, so nothing at risk here was ever verified.
    """
    readme = deps.store.read_subtask_readme(ticket.identifier, subtask.id)
    siblings = [(st.id, st.log or "") for st in state.subtasks if st.is_done()]
    total = max(1, deps.attempts)
    result = SpawnResult(status=SubTaskStatus.FAILED.value, error="spawn never ran")
    last_error: str | None = None

    for attempt in range(1, total + 1):
        result = deps.spawn(
            ticket=ticket,
            subtask=subtask,
            readme=readme,
            siblings=siblings,
            attempt=attempt,
            previous_error=last_error,
        )
        if result.status != SubTaskStatus.FAILED.value:
            return result

        last_error = result.error
        if attempt < total:
            deps.store.append_execution_log(
                ticket.identifier,
                subtask.id,
                f"[foreman] attempt {attempt} of {total} failed: {result.error}. "
                "Retrying once in the same working tree.",
            )
            # Re-read: the failed attempt may have appended to the log itself,
            # and the retry should see whatever it left behind.
            readme = deps.store.read_subtask_readme(ticket.identifier, subtask.id)

    if total > 1:
        result.error = f"{result.error} (after {total} attempts)"
    return result


def _finalize(deps: Deps, ticket: Ticket, state: TicketState) -> RunReport:
    """Open the PR and stop, or report what got in the way.

    This is the gate. Nothing past this point is automated.
    """
    if not state.all_done():
        deps.store.save(state)
        deps.linear.comment(ticket.identifier, halt_comment(ticket, state))
        return RunReport(
            ticket=ticket.identifier,
            outcome="halted",
            branch=state.branch,
            detail="sub-tasks remain blocked, failed, or unreachable",
            subtasks=_subtask_rows(state),
        )

    deps.git.commit(f"{ticket.identifier}: {ticket.title}")
    deps.git.push(state.branch)

    base = deps.git.default_branch()
    pr = deps.git.open_pull_request(
        branch=state.branch,
        base=base,
        title=f"{ticket.identifier}: {ticket.title}",
        body=pr_body(ticket, state),
    )

    state.pr_url = pr.url
    state.status = TicketStatus.IN_REVIEW.value
    deps.store.save(state)

    notes: list[str] = []
    if pr.manual:
        deps.linear.comment(
            ticket.identifier,
            f"Foreman finished {ticket.identifier} on branch `{state.branch}` but "
            "could not open the pull request automatically. Open it by hand:\n\n"
            f"{pr.body}",
        )
    else:
        deps.linear.comment(ticket.identifier, f"Pull request opened: {pr.url}")

    # The real gate is the open PR plus the comment on the ticket, and both have
    # already happened by this point. Moving the ticket is a convenience on top
    # of that, so a board with no matching workflow state must not turn a
    # finished run into a failed one. Report it and carry on.
    try:
        deps.linear.set_status(ticket.identifier, TicketStatus.IN_REVIEW.value)
    except Exception as exc:  # noqa: BLE001 - see comment above
        notes.append(f"could not move the ticket to in-review: {exc}")

    return RunReport(
        ticket=ticket.identifier,
        outcome="in_review",
        branch=state.branch,
        pr_url=pr.url,
        detail="stopped at the human gate: PR open, ticket in review",
        notes=notes,
        subtasks=_subtask_rows(state),
    )
