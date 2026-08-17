"""Moving the ticket into the in-flight column while the run happens.

Nothing in the pipeline reads this back. It exists for everyone who is not
watching the terminal: while a run is going, the ticket is the only place a
colleague can see the work is already underway. So the rule is that it happens
early and that it can never take a run down with it.
"""

from test_orchestrator_stub import TWO_STEP, FakeGit, make_deps

from forman.models import SpawnResult, Ticket, TicketStatus
from forman.orchestrator import run_once


def test_the_ticket_moves_before_any_work_is_attempted(tmp_path):
    """Before decomposition, which is a model session that can run for minutes.

    Moving it afterwards would leave the board silent for exactly the stretch
    where somebody is most likely to go and pick the ticket up themselves.
    """
    order: list[str] = []
    deps, _store, linear, _git = make_deps(tmp_path, TWO_STEP)

    real_decompose = deps.decompose
    real_set_status = linear.set_status

    def decompose(ticket):
        order.append("decompose")
        return real_decompose(ticket)

    def set_status(identifier, status):
        order.append(f"status:{status}")
        real_set_status(identifier, status)

    deps.decompose = decompose
    linear.set_status = set_status

    run_once(deps)

    assert order[0] == "status:in_progress"
    assert order.index("status:in_progress") < order.index("decompose")


def test_a_halted_run_leaves_the_ticket_in_progress(tmp_path):
    """Not walked back to todo. A halted ticket is still this run's work, and
    the in-flight column is the signal that somebody should come and look."""
    results = {"TEAM-7.01": SpawnResult(status="blocked", blocked_reason="no key")}
    deps, _store, linear, _git = make_deps(tmp_path, TWO_STEP, results)

    assert run_once(deps).outcome == "halted"
    assert linear.tickets["TEAM-7"].status == TicketStatus.IN_PROGRESS.value


def test_resuming_a_ticket_already_in_progress_does_not_move_it_again(tmp_path):
    """Re-running is routine. Each redundant move is a write on somebody's
    board, and shows up in Linear's activity feed as if something happened."""
    ticket = Ticket(identifier="TEAM-7", title="Add rate limiting to the auth endpoint")
    results = {"TEAM-7.01": SpawnResult(status="blocked", blocked_reason="no key")}
    deps, store, linear, _git = make_deps(tmp_path, TWO_STEP, results, tickets=[ticket])

    run_once(deps)
    assert linear.status_changes == [("TEAM-7", "in_progress")]

    # Second run: same ticket, already in the in-flight column.
    store.load("TEAM-7")
    deps.git = FakeGit()
    run_once(deps)

    assert linear.status_changes == [("TEAM-7", "in_progress")]


def test_the_forced_path_moves_the_ticket_too(tmp_path):
    tickets = [
        Ticket(identifier="TEAM-30", title="Not this one", priority="urgent"),
        Ticket(identifier="TEAM-31", title="This one"),
    ]
    goals = [("do the thing", [])]
    deps, _store, linear, _git = make_deps(tmp_path, goals, tickets=tickets)

    run_once(deps, ticket_id="TEAM-31")

    assert ("TEAM-31", "in_progress") in linear.status_changes
    assert not any(t == "TEAM-30" for t, _ in linear.status_changes)
