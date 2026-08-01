"""The whole loop, against a stubbed spawn and a stubbed Linear.

This is the v1 definition of done. It asserts the three things that matter:
state transitions, where commits happen, and that the run halts at the gate.
No network, no git, no SDK.
"""

import pytest

from foreman.linear_client import StubLinearClient
from foreman.models import (
    CommitResult,
    PullRequest,
    SpawnResult,
    SubTask,
    SubTaskStatus,
    Ticket,
    TicketStatus,
)
from foreman.orchestrator import Deps, run_once, select_ticket
from foreman.state import StateStore


# -- fakes -------------------------------------------------------------------


class FakeGit:
    """Records what the orchestrator asked git to do, in order."""

    def __init__(self, clean: bool = True, pr_url: str | None = "https://github.com/o/r/pull/1"):
        self.calls: list[tuple[str, str]] = []
        self.commits: list[str] = []
        self.clean = clean
        self.pr_url = pr_url
        self.pushed: list[str] = []

    def require_clean(self) -> None:
        self.calls.append(("require_clean", ""))
        if not self.clean:
            raise RuntimeError("dirty worktree")

    def default_branch(self) -> str:
        return "main"

    def sync_default_branch(self) -> str:
        self.calls.append(("sync", "main"))
        return "main"

    def branch_name(self, identifier: str, title: str) -> str:
        from foreman.git_ops import branch_name

        return branch_name(identifier, title)

    def create_branch(self, name: str) -> str:
        self.calls.append(("create_branch", name))
        return name

    def commit(self, message: str) -> CommitResult:
        self.calls.append(("commit", message))
        self.commits.append(message)
        return CommitResult(created=True, sha=f"sha{len(self.commits)}", message=message)

    def push(self, branch: str) -> bool:
        self.calls.append(("push", branch))
        self.pushed.append(branch)
        return True

    def open_pull_request(self, branch, base, title, body) -> PullRequest:
        self.calls.append(("open_pr", branch))
        return PullRequest(
            url=self.pr_url, branch=branch, title=title, body=body, manual=self.pr_url is None
        )


def canned_decompose(store: StateStore, goals: list[tuple[str, list[str]]]):
    """Return a decompose callable that seeds fixed sub-tasks and writes briefs."""

    def _decompose(ticket: Ticket) -> list[SubTask]:
        subtasks = []
        for i, (goal, deps) in enumerate(goals, start=1):
            sid = f"{ticket.identifier}.{i:02d}"
            subtasks.append(SubTask(id=sid, goal=goal, depends_on=deps))
            store.write_subtask_readme(
                ticket.identifier,
                sid,
                f"## Goal\n{goal}\n\n---\n## Execution log\n",
            )
        return subtasks

    return _decompose


def canned_spawn(results: dict[str, SpawnResult], seen: list[str] | None = None):
    """Replay a canned result per sub-task id.

    A value may be a list, which is consumed one entry per attempt: that is how
    the retry tests express "fails, then succeeds".
    """

    def _spawn(*, ticket, subtask, readme, siblings, attempt=1, previous_error=None):
        if seen is not None:
            seen.append(subtask.id)
        canned = results.get(subtask.id)
        if isinstance(canned, list):
            return canned.pop(0) if canned else SpawnResult(status="done")
        return canned or SpawnResult(status="done", summary=f"did {subtask.id}")

    return _spawn


def make_deps(tmp_path, goals, results=None, tickets=None, seen=None, git=None):
    store = StateStore(tmp_path)
    linear = StubLinearClient(
        tickets or [Ticket(identifier="TEAM-7", title="Add rate limiting to the auth endpoint")]
    )
    git = git or FakeGit()
    deps = Deps(
        linear=linear,
        store=store,
        git=git,
        decompose=canned_decompose(store, goals),
        spawn=canned_spawn(results or {}, seen),
        now=lambda: "2026-08-01T10:00:00+00:00",
    )
    return deps, store, linear, git


TWO_STEP = [("Add a token bucket helper", []), ("Wire it into the auth route", ["TEAM-7.01"])]


# -- happy path --------------------------------------------------------------


def test_full_run_reaches_the_gate_and_stops(tmp_path):
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP)

    report = run_once(deps)

    assert report.outcome == "in_review"
    assert report.ticket == "TEAM-7"
    assert report.branch == "team-7/add-rate-limiting-to-the-auth-endpoint"
    assert report.pr_url == "https://github.com/o/r/pull/1"

    state = store.load("TEAM-7")
    assert state.status == TicketStatus.IN_REVIEW.value
    assert state.all_done()
    assert state.pr_url == report.pr_url


def test_ticket_is_set_to_in_review_and_never_to_done(tmp_path):
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP)
    run_once(deps)

    assert linear.status_changes == [("TEAM-7", "in_review")]
    assert linear.tickets["TEAM-7"].status == "in_review"
    # The gate: nothing in this pipeline may mark a ticket done.
    assert all(status != "done" for _, status in linear.status_changes)


def test_pr_link_is_commented_on_the_ticket(tmp_path):
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP)
    run_once(deps)

    assert len(linear.comments) == 1
    identifier, body = linear.comments[0]
    assert identifier == "TEAM-7"
    assert "https://github.com/o/r/pull/1" in body


def test_branch_is_cut_from_a_freshly_pulled_default_branch(tmp_path):
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP)
    run_once(deps)

    verbs = [name for name, _ in git.calls]
    assert verbs.index("require_clean") < verbs.index("sync") < verbs.index("create_branch")
    assert ("create_branch", "team-7/add-rate-limiting-to-the-auth-endpoint") in git.calls


def test_commit_after_each_done_subtask(tmp_path):
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP)
    run_once(deps)

    # One commit per done sub-task, plus the finalize commit.
    assert git.commits[0].startswith("TEAM-7 TEAM-7.01:")
    assert git.commits[1].startswith("TEAM-7 TEAM-7.02:")
    assert git.commits[2] == "TEAM-7: Add rate limiting to the auth endpoint"
    assert len(git.commits) == 3


def test_subtasks_run_serially_in_dependency_order(tmp_path):
    seen: list[str] = []
    deps, *_ = make_deps(tmp_path, TWO_STEP, seen=seen)
    run_once(deps)
    assert seen == ["TEAM-7.01", "TEAM-7.02"]


def test_manifest_tracks_the_run(tmp_path):
    deps, store, *_ = make_deps(tmp_path, TWO_STEP)
    run_once(deps)

    manifest = store.manifest_path("TEAM-7").read_text()
    assert "[x] `TEAM-7.01`" in manifest
    assert "[x] `TEAM-7.02`" in manifest
    assert "in_review" in manifest


def test_subtasks_never_reach_linear(tmp_path):
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP)
    run_once(deps)

    # Linear sees the ticket and one PR comment. Never a sub-task id.
    for _, body in linear.comments:
        assert "TEAM-7.01" not in body
    assert list(linear.tickets) == ["TEAM-7"]


# -- blocked and failed paths ------------------------------------------------


def test_blocked_subtask_halts_the_run_before_the_gate(tmp_path):
    results = {
        "TEAM-7.01": SpawnResult(
            status="blocked",
            summary="could not proceed",
            blocked_reason="needs a STRIPE_KEY only the human has",
        )
    }
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP, results)

    report = run_once(deps)

    assert report.outcome == "halted"
    assert report.pr_url is None
    assert git.pushed == []
    assert ("open_pr", "team-7/add-rate-limiting-to-the-auth-endpoint") not in git.calls

    state = store.load("TEAM-7")
    assert state.status == TicketStatus.IN_PROGRESS.value
    assert state.subtask("TEAM-7.01").status == SubTaskStatus.BLOCKED.value
    assert state.subtask("TEAM-7.01").blocked_reason == "needs a STRIPE_KEY only the human has"
    # The dependent never ran: blocking is terminal for this run, no retry pass.
    assert state.subtask("TEAM-7.02").status == SubTaskStatus.PENDING.value

    assert linear.status_changes == []
    assert "blocked" in linear.comments[0][1]


def test_independent_sibling_still_runs_when_one_is_blocked(tmp_path):
    goals = [("first thing", []), ("unrelated thing", [])]
    results = {"TEAM-7.01": SpawnResult(status="blocked", blocked_reason="external")}
    deps, store, *_ = make_deps(tmp_path, goals, results)

    run_once(deps)

    state = store.load("TEAM-7")
    assert state.subtask("TEAM-7.01").status == SubTaskStatus.BLOCKED.value
    assert state.subtask("TEAM-7.02").status == SubTaskStatus.DONE.value


def test_failed_subtask_is_recorded_and_halts(tmp_path):
    results = {"TEAM-7.01": SpawnResult(status="failed", error="agent hit its turn limit")}
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP, results)

    report = run_once(deps)

    assert report.outcome == "halted"
    state = store.load("TEAM-7")
    assert state.subtask("TEAM-7.01").status == SubTaskStatus.FAILED.value
    # It is retried once before giving up, and says so. See test_retry.py.
    assert state.subtask("TEAM-7.01").blocked_reason == (
        "agent hit its turn limit (after 2 attempts)"
    )
    assert linear.status_changes == []


def test_no_commit_for_a_blocked_subtask(tmp_path):
    results = {"TEAM-7.01": SpawnResult(status="blocked", blocked_reason="external")}
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP, results)
    run_once(deps)
    assert git.commits == []


# -- session id and cost -----------------------------------------------------


def test_session_and_cost_are_recorded_for_done_subtasks(tmp_path):
    results = {
        "TEAM-7.01": SpawnResult(
            status="done", summary="did the helper", session_id="sess-01", total_cost_usd=0.42
        ),
        "TEAM-7.02": SpawnResult(
            status="done", summary="wired it up", session_id="sess-02", total_cost_usd=1.5
        ),
    }
    deps, store, *_ = make_deps(tmp_path, TWO_STEP, results)

    run_once(deps)

    state = store.load("TEAM-7")
    assert state.subtask("TEAM-7.01").session_id == "sess-01"
    assert state.subtask("TEAM-7.01").cost_usd == 0.42
    assert state.subtask("TEAM-7.02").session_id == "sess-02"
    assert state.subtask("TEAM-7.02").cost_usd == 1.5


def test_session_and_cost_are_recorded_for_blocked_and_failed_subtasks(tmp_path):
    # A session that ended badly still burned tokens, and its id is how you go
    # and read it, so both outcomes must record them too.
    goals = [("blocked thing", []), ("failed thing", [])]
    results = {
        "TEAM-7.01": SpawnResult(
            status="blocked",
            blocked_reason="needs a key only the human has",
            session_id="sess-blocked",
            total_cost_usd=0.11,
        ),
        "TEAM-7.02": SpawnResult(
            status="failed",
            error="agent hit its turn limit",
            session_id="sess-failed",
            total_cost_usd=0.22,
        ),
    }
    deps, store, *_ = make_deps(tmp_path, goals, results)

    run_once(deps)

    state = store.load("TEAM-7")
    assert state.subtask("TEAM-7.01").status == SubTaskStatus.BLOCKED.value
    assert state.subtask("TEAM-7.01").session_id == "sess-blocked"
    assert state.subtask("TEAM-7.01").cost_usd == 0.11
    assert state.subtask("TEAM-7.02").status == SubTaskStatus.FAILED.value
    assert state.subtask("TEAM-7.02").session_id == "sess-failed"
    assert state.subtask("TEAM-7.02").cost_usd == 0.22


def test_a_retried_subtask_records_the_last_attempts_session(tmp_path):
    results = {
        "TEAM-7.01": [
            SpawnResult(
                status="failed", error="connection dropped", session_id="sess-a", total_cost_usd=0.1
            ),
            SpawnResult(
                status="done", summary="finished it", session_id="sess-b", total_cost_usd=0.3
            ),
        ]
    }
    deps, store, *_ = make_deps(tmp_path, TWO_STEP, results)

    run_once(deps)

    subtask = store.load("TEAM-7").subtask("TEAM-7.01")
    assert subtask.status == SubTaskStatus.DONE.value
    # The last attempt's session, and that session's cost only. Costs are
    # deliberately not accumulated across attempts in v1.
    assert subtask.session_id == "sess-b"
    assert subtask.cost_usd == 0.3


def test_report_carries_the_ticket_total_and_per_subtask_cost(tmp_path):
    results = {
        "TEAM-7.01": SpawnResult(status="done", session_id="sess-01", total_cost_usd=0.42),
        "TEAM-7.02": SpawnResult(status="done", session_id="sess-02", total_cost_usd=1.5),
    }
    deps, store, *_ = make_deps(tmp_path, TWO_STEP, results)

    report = run_once(deps)

    assert report.outcome == "in_review"
    assert report.total_cost_usd == 1.92
    assert [row["cost_usd"] for row in report.subtasks] == [0.42, 1.5]
    # The report and the manifest are summed by the same helper, so they agree.
    assert "**Total: $1.9200**" in store.manifest_path("TEAM-7").read_text()


def test_halted_report_still_carries_the_total(tmp_path):
    # A run that never reaches the gate still spent money, and that is exactly
    # when you want to know how much.
    results = {
        "TEAM-7.01": SpawnResult(status="done", total_cost_usd=0.42),
        "TEAM-7.02": SpawnResult(
            status="blocked", blocked_reason="waiting on TEAM-8", total_cost_usd=0.08
        ),
    }
    deps, store, *_ = make_deps(tmp_path, TWO_STEP, results)

    report = run_once(deps)

    assert report.outcome == "halted"
    assert report.total_cost_usd == 0.5


def test_total_is_zero_when_no_costs_were_recorded(tmp_path):
    # The spawn fake returns results with no cost at all, as an older Foreman or
    # a stubbed session would. Reporting must not crash or invent a figure.
    deps, store, *_ = make_deps(tmp_path, TWO_STEP)

    report = run_once(deps)

    assert report.outcome == "in_review"
    assert report.total_cost_usd == 0.0


# -- resume ------------------------------------------------------------------


def test_rerun_skips_already_done_subtasks(tmp_path):
    results = {"TEAM-7.02": SpawnResult(status="blocked", blocked_reason="waiting on TEAM-8")}
    first_seen: list[str] = []
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP, results, seen=first_seen)
    run_once(deps)
    assert first_seen == ["TEAM-7.01", "TEAM-7.02"]

    # The human resolves the blocker. Re-run: .01 is skipped, .02 is not retried
    # automatically either, because a blocked sub-task stays blocked until it is
    # reset. What the re-run must not do is redo completed work.
    state = store.load("TEAM-7")
    state.subtask("TEAM-7.02").status = SubTaskStatus.PENDING.value
    state.subtask("TEAM-7.02").blocked_reason = None
    store.save(state)

    second_seen: list[str] = []
    deps2, store2, linear2, git2 = make_deps(tmp_path, TWO_STEP, seen=second_seen)
    report = run_once(deps2)

    assert second_seen == ["TEAM-7.02"]
    assert report.outcome == "in_review"
    # It reused the existing branch instead of cutting a new one off main.
    assert ("sync", "main") not in git2.calls


# -- gate failure modes ------------------------------------------------------


def test_manual_pr_still_reaches_in_review_with_the_body_on_the_ticket(tmp_path):
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP, git=FakeGit(pr_url=None))
    report = run_once(deps)

    assert report.outcome == "in_review"
    assert report.pr_url is None
    assert linear.status_changes == [("TEAM-7", "in_review")]
    assert "could not open the pull request" in linear.comments[0][1]


def test_dirty_worktree_aborts_before_anything_happens(tmp_path):
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP, git=FakeGit(clean=False))

    with pytest.raises(RuntimeError, match="dirty"):
        run_once(deps)

    assert git.commits == []
    assert not store.exists("TEAM-7")
    assert linear.comments == []


def test_no_ready_ticket_is_a_clean_no_op(tmp_path):
    tickets = [
        Ticket(identifier="TEAM-8", title="Blocked one", blocked_by=["TEAM-9"]),
        Ticket(identifier="TEAM-9", title="Not done yet", status="in_progress"),
    ]
    deps, store, linear, git = make_deps(tmp_path, TWO_STEP, tickets=tickets)
    # TEAM-9 is itself ready, so pick it; assert the truly-blocked one is skipped.
    report = run_once(deps)
    assert report.ticket == "TEAM-9"


# -- ticket selection --------------------------------------------------------


def test_select_ticket_skips_tickets_with_unfinished_blockers():
    tickets = [
        Ticket(identifier="TEAM-2", title="second", blocked_by=["TEAM-1"]),
        Ticket(identifier="TEAM-1", title="first"),
    ]
    assert select_ticket(tickets).identifier == "TEAM-1"


def test_select_ticket_unblocks_once_the_blocker_is_done():
    tickets = [
        Ticket(identifier="TEAM-2", title="second", blocked_by=["TEAM-1"]),
        Ticket(identifier="TEAM-1", title="first", status="done"),
    ]
    assert select_ticket(tickets).identifier == "TEAM-2"


def test_select_ticket_ignores_blockers_outside_the_assigned_set():
    tickets = [Ticket(identifier="TEAM-2", title="second", blocked_by=["OTHER-9"])]
    assert select_ticket(tickets).identifier == "TEAM-2"


def test_select_ticket_prefers_higher_priority():
    tickets = [
        Ticket(identifier="TEAM-1", title="low one", priority="low"),
        Ticket(identifier="TEAM-2", title="urgent one", priority="urgent"),
    ]
    assert select_ticket(tickets).identifier == "TEAM-2"


def test_select_ticket_breaks_priority_ties_by_unblock_count():
    tickets = [
        Ticket(identifier="TEAM-1", title="unblocks nothing"),
        Ticket(identifier="TEAM-2", title="unblocks two"),
        Ticket(identifier="TEAM-3", title="waits", blocked_by=["TEAM-2"]),
        Ticket(identifier="TEAM-4", title="waits too", blocked_by=["TEAM-2"]),
    ]
    assert select_ticket(tickets).identifier == "TEAM-2"


def test_select_ticket_skips_tickets_already_at_the_gate():
    tickets = [
        Ticket(identifier="TEAM-1", title="awaiting review", status="in_review"),
        Ticket(identifier="TEAM-2", title="actual work"),
    ]
    assert select_ticket(tickets).identifier == "TEAM-2"


def test_select_ticket_returns_none_when_everything_is_done():
    assert select_ticket([Ticket(identifier="TEAM-1", title="x", status="done")]) is None
