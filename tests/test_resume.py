"""Resuming a halted run, and not retrying failures that cannot be retried.

Both come from a real halt: a run died on a Claude usage limit, burned its
retry against the same exhausted limit, and then could not be resumed at all
because nothing moves a failed sub-task back to pending.
"""

import pytest

from foreman.models import SpawnResult, SubTask, SubTaskStatus, TicketState
from foreman.orchestrator import run_once
from foreman.spawn import AgentRun, is_retryable, result_from_run
from foreman.state import StateStore, next_ready_subtask, reset_for_resume

from test_orchestrator_stub import TWO_STEP, make_deps
from test_retry import RecordingSpawn


def halted_state() -> TicketState:
    return TicketState(
        ticket="TEAM-35",
        title="Persist and report agent session cost",
        status="in_progress",
        branch="team-35/persist-cost",
        subtasks=[
            SubTask(id="TEAM-35.01", goal="one", status=SubTaskStatus.DONE.value, log="did it"),
            SubTask(
                id="TEAM-35.02",
                goal="two",
                status=SubTaskStatus.FAILED.value,
                blocked_reason="usage limit reached (after 2 attempts)",
                started_at="t1",
                finished_at="t2",
            ),
            SubTask(id="TEAM-35.03", goal="three", depends_on=["TEAM-35.02"]),
        ],
    )


# -- what a halt looks like before resume ------------------------------------


def test_a_failed_subtask_blocks_the_run_forever_without_resume():
    state = halted_state()
    # .02 failed, so it is not pending; .03 depends on it. Nothing can run.
    assert next_ready_subtask(state) is None


# -- resume ------------------------------------------------------------------


def test_resume_puts_failed_work_back_in_play():
    state = halted_state()
    changed = reset_for_resume(state)

    assert changed == [("TEAM-35.02", "failed")]
    assert next_ready_subtask(state).id == "TEAM-35.02"


def test_resume_never_touches_completed_work():
    state = halted_state()
    reset_for_resume(state)

    done = state.subtask("TEAM-35.01")
    assert done.status == SubTaskStatus.DONE.value
    assert done.log == "did it"


def test_resume_clears_the_stale_failure_details():
    state = halted_state()
    reset_for_resume(state)

    failed = state.subtask("TEAM-35.02")
    assert failed.blocked_reason is None
    assert failed.started_at is None and failed.finished_at is None


def test_resume_takes_blocked_subtasks_too_by_default():
    state = halted_state()
    state.subtask("TEAM-35.03").status = SubTaskStatus.BLOCKED.value
    state.subtask("TEAM-35.03").blocked_reason = "needed a credential"

    changed = dict(reset_for_resume(state))

    assert changed == {"TEAM-35.02": "failed", "TEAM-35.03": "blocked"}


def test_resume_can_leave_blocked_subtasks_alone():
    state = halted_state()
    state.subtask("TEAM-35.03").status = SubTaskStatus.BLOCKED.value

    changed = dict(reset_for_resume(state, include_blocked=False))

    assert changed == {"TEAM-35.02": "failed"}
    assert state.subtask("TEAM-35.03").status == SubTaskStatus.BLOCKED.value


def test_resume_is_a_no_op_on_a_healthy_ticket():
    state = halted_state()
    state.subtask("TEAM-35.02").status = SubTaskStatus.DONE.value
    state.subtask("TEAM-35.03").status = SubTaskStatus.DONE.value

    assert reset_for_resume(state) == []


def test_a_resumed_run_only_reruns_what_failed(tmp_path):
    store = StateStore(tmp_path)
    store.save(halted_state())
    for st in ("TEAM-35.01", "TEAM-35.02", "TEAM-35.03"):
        store.write_subtask_readme("TEAM-35", st, f"## Goal\n{st}\n\n---\n## Execution log\n")

    state = store.load("TEAM-35")
    reset_for_resume(state)
    store.save(state)

    spawn = RecordingSpawn({})
    deps, _store, linear, git = make_deps(tmp_path, TWO_STEP)
    deps.store = store
    deps.spawn = spawn
    from foreman.models import Ticket

    deps.linear.tickets = {
        "TEAM-35": Ticket(identifier="TEAM-35", title="Persist and report agent session cost")
    }

    run_once(deps, ticket_id="TEAM-35")

    # .01 was already done and is not run again.
    assert [c["id"] for c in spawn.calls] == ["TEAM-35.02", "TEAM-35.03"]


# -- failures that a retry cannot fix ----------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        "Claude usage limit reached",
        "rate limit exceeded",
        "HTTP 429 Too Many Requests",
        "your credit balance is too low",
        "authentication_error: invalid api key",
        "permission denied",
    ],
)
def test_limits_and_credentials_are_not_retryable(error):
    assert is_retryable(error) is False


@pytest.mark.parametrize(
    "error",
    [
        "ConnectionError: dropped",
        "agent hit its turn limit before finishing",
        "TimeoutError",
        None,
    ],
)
def test_transient_and_unknown_failures_stay_retryable(error):
    # Unknown errors get the benefit of the doubt: a wasted retry is cheaper
    # than refusing to retry something that would have worked.
    assert is_retryable(error) is True


def test_result_from_run_marks_a_usage_limit_permanent():
    result = result_from_run(AgentRun(error="Claude usage limit reached"))
    assert result.status == SubTaskStatus.FAILED.value
    assert result.retryable is False


def test_a_usage_limit_is_not_retried(tmp_path):
    spawn = RecordingSpawn(
        {
            "TEAM-7.01": [
                SpawnResult(
                    status="failed",
                    error="Claude usage limit reached",
                    retryable=False,
                )
            ]
        }
    )
    deps, store, *_ = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn

    run_once(deps)

    # One attempt only. The limit will still be there in ten seconds.
    assert [c["attempt"] for c in spawn.calls if c["id"] == "TEAM-7.01"] == [1]
    # And it is reported bare, without the "(after 2 attempts)" suffix.
    assert store.load("TEAM-7").subtask("TEAM-7.01").blocked_reason == (
        "Claude usage limit reached"
    )


def test_a_transient_failure_is_still_retried(tmp_path):
    spawn = RecordingSpawn(
        {"TEAM-7.01": [SpawnResult(status="failed", error="ConnectionError: dropped")]}
    )
    deps, *_ = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn

    run_once(deps)

    assert [c["attempt"] for c in spawn.calls if c["id"] == "TEAM-7.01"] == [1, 2]


# -- porcelain parsing -------------------------------------------------------


def test_dirty_file_names_are_not_truncated(tmp_path):
    """git_ops._run strips the whole output, which eats the leading space of
    the first porcelain line. Slicing a fixed 3 chars then loses a character
    off the first filename, in every dirty-tree message."""
    import subprocess

    from foreman.git_ops import worktree_status

    def sh(*args):
        subprocess.run(args, cwd=tmp_path, capture_output=True, check=True)

    sh("git", "init", "-q")
    sh("git", "config", "user.email", "t@example.com")
    sh("git", "config", "user.name", "Test")
    (tmp_path / "alpha.py").write_text("one\n")
    (tmp_path / "beta.py").write_text("two\n")
    sh("git", "add", "-A")
    sh("git", "commit", "-qm", "init")

    (tmp_path / "alpha.py").write_text("changed\n")
    (tmp_path / "beta.py").write_text("changed\n")

    status = worktree_status(tmp_path)
    assert not status.clean
    assert sorted(status.files) == ["alpha.py", "beta.py"]
