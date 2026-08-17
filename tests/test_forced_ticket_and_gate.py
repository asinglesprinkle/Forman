"""Forcing a specific ticket, and surviving a board with no review state.

Both of these exist because of what `forman doctor` turned up on a real
workspace: left to itself the selector would have grabbed live work, and plenty
of boards have no in-review state for the gate to move a ticket into.
"""

from test_orchestrator_stub import TWO_STEP, make_deps

from forman.linear_client import StubLinearClient
from forman.models import Ticket, TicketStatus
from forman.orchestrator import run_once


class NoReviewStateLinear(StubLinearClient):
    """A board whose workflow has nowhere called anything like 'in review'."""

    def set_status(self, identifier: str, status: str) -> None:
        raise RuntimeError(
            f"no workflow state on team TEAM matching {status!r}. "
            "Available: In Progress, Backlog, Todo, Canceled, Done, Duplicate"
        )


def test_forced_ticket_beats_the_selector(tmp_path):
    tickets = [
        Ticket(identifier="TEAM-30", title="Real work in progress", priority="urgent"),
        Ticket(identifier="TEAM-31", title="Forman smoke test", priority="low"),
    ]
    deps, _store, _linear, _git = make_deps(tmp_path, TWO_STEP, tickets=tickets)

    # Left alone, the selector takes the urgent one.
    assert run_once(deps).ticket == "TEAM-30"


def test_forced_ticket_is_worked_even_when_lower_priority(tmp_path):
    tickets = [
        Ticket(identifier="TEAM-30", title="Real work in progress", priority="urgent"),
        Ticket(identifier="TEAM-31", title="Forman smoke test", priority="low"),
    ]
    goals = [("do the thing", []), ("do the other thing", ["TEAM-31.01"])]
    deps, store, _linear, _git = make_deps(tmp_path, goals, tickets=tickets)

    report = run_once(deps, ticket_id="TEAM-31")

    assert report.ticket == "TEAM-31"
    assert report.outcome == "in_review"
    assert report.branch == "team-31/forman-smoke-test"
    # The urgent ticket was never touched.
    assert not store.exists("TEAM-30")


def test_forcing_a_finished_ticket_is_a_no_op(tmp_path):
    tickets = [Ticket(identifier="TEAM-31", title="Already shipped", status="done")]
    deps, _store, _linear, git = make_deps(tmp_path, TWO_STEP, tickets=tickets)

    report = run_once(deps, ticket_id="TEAM-31")

    assert report.outcome == "no_work"
    assert "already finished" in report.detail
    assert git.commits == []


def test_missing_review_state_does_not_fail_a_finished_run(tmp_path):
    deps, store, _linear, _git = make_deps(tmp_path, TWO_STEP)
    deps.linear = NoReviewStateLinear(
        [Ticket(identifier="TEAM-7", title="Add rate limiting to the auth endpoint")]
    )

    report = run_once(deps)

    # The real gate happened: PR open, link commented, local state at in_review.
    assert report.outcome == "in_review"
    assert report.pr_url == "https://github.com/o/r/pull/1"
    assert store.load("TEAM-7").status == TicketStatus.IN_REVIEW.value
    assert any("Pull request opened" in body for _, body in deps.linear.comments)

    # And the failure is surfaced rather than swallowed. This board also has no
    # state the in-progress move can find, so both are reported.
    notes = "\n".join(report.notes)
    assert "in-review" in notes
    assert "in-progress" in notes
    assert "Available: In Progress" in notes


def test_missing_in_progress_state_does_not_stop_the_run(tmp_path):
    """The board move is a courtesy to onlookers, not a precondition."""
    deps, store, _linear, git = make_deps(tmp_path, TWO_STEP)
    deps.linear = NoReviewStateLinear(
        [Ticket(identifier="TEAM-7", title="Add rate limiting to the auth endpoint")]
    )

    report = run_once(deps)

    # Every sub-task ran and the PR opened, despite the very first Linear write
    # of the run blowing up.
    assert report.outcome == "in_review"
    assert store.load("TEAM-7").all_done()
    assert git.commits
    assert any("in-progress" in note for note in report.notes)
