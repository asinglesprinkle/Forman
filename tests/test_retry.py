"""One retry on `failed`, never on `blocked`.

A turn-limit hit or a dropped connection is usually transient, so halting a
whole ticket over one is needless. An external blocker will not resolve itself,
so retrying it just burns tokens to get the same answer.
"""

from test_orchestrator_stub import TWO_STEP, make_deps

from forman.models import SpawnResult, SubTaskStatus, Ticket
from forman.orchestrator import run_once
from forman.spawn import build_subtask_prompt


class RecordingSpawn:
    """Replays a canned result per attempt and records how it was called."""

    def __init__(self, per_attempt: dict[str, list[SpawnResult]]):
        self.per_attempt = per_attempt
        self.calls: list[dict] = []

    def __call__(self, *, ticket, subtask, readme, siblings, attempt, previous_error):
        self.calls.append(
            {
                "id": subtask.id,
                "attempt": attempt,
                "previous_error": previous_error,
                "readme": readme,
            }
        )
        canned = self.per_attempt.get(subtask.id)
        if canned:
            return canned.pop(0)
        return SpawnResult(status="done", summary=f"did {subtask.id}")


def test_a_failed_subtask_is_retried_once_and_can_succeed(tmp_path):
    spawn = RecordingSpawn(
        {"TEAM-7.01": [SpawnResult(status="failed", error="turn limit hit")]}
    )
    deps, store, _linear, _git = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn

    report = run_once(deps)

    attempts = [c for c in spawn.calls if c["id"] == "TEAM-7.01"]
    assert [c["attempt"] for c in attempts] == [1, 2]
    assert report.outcome == "in_review"
    assert store.load("TEAM-7").subtask("TEAM-7.01").status == SubTaskStatus.DONE.value


def test_the_retry_is_told_why_the_first_attempt_died(tmp_path):
    spawn = RecordingSpawn(
        {"TEAM-7.01": [SpawnResult(status="failed", error="turn limit hit")]}
    )
    deps, *_ = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn
    run_once(deps)

    first, second = [c for c in spawn.calls if c["id"] == "TEAM-7.01"][:2]
    assert first["previous_error"] is None
    assert second["previous_error"] == "turn limit hit"


def test_the_retry_sees_what_the_failed_attempt_left_in_the_log(tmp_path):
    spawn = RecordingSpawn(
        {"TEAM-7.01": [SpawnResult(status="failed", error="turn limit hit")]}
    )
    deps, *_ = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn
    run_once(deps)

    second = [c for c in spawn.calls if c["id"] == "TEAM-7.01"][1]
    assert "attempt 1 of 2 failed" in second["readme"]


def test_two_failures_give_up_and_say_how_many_attempts(tmp_path):
    spawn = RecordingSpawn(
        {
            "TEAM-7.01": [
                SpawnResult(status="failed", error="turn limit hit"),
                SpawnResult(status="failed", error="turn limit hit again"),
            ]
        }
    )
    deps, store, linear, _git = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn

    report = run_once(deps)

    assert [c["attempt"] for c in spawn.calls if c["id"] == "TEAM-7.01"] == [1, 2]
    assert report.outcome == "halted"
    failed = store.load("TEAM-7").subtask("TEAM-7.01")
    assert failed.status == SubTaskStatus.FAILED.value
    assert "after 2 attempts" in failed.blocked_reason
    assert linear.status_changes == []


def test_a_blocked_subtask_is_never_retried(tmp_path):
    spawn = RecordingSpawn(
        {"TEAM-7.01": [SpawnResult(status="blocked", blocked_reason="needs a key")]}
    )
    deps, store, *_ = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn

    run_once(deps)

    # Exactly one attempt. Retrying an external blocker only burns tokens.
    assert [c["attempt"] for c in spawn.calls if c["id"] == "TEAM-7.01"] == [1]
    assert store.load("TEAM-7").subtask("TEAM-7.01").blocked_reason == "needs a key"


def test_a_successful_subtask_is_not_retried(tmp_path):
    spawn = RecordingSpawn({})
    deps, *_ = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn

    run_once(deps)

    assert [c["attempt"] for c in spawn.calls] == [1, 1]  # one per sub-task


def test_retries_can_be_turned_off(tmp_path):
    spawn = RecordingSpawn(
        {"TEAM-7.01": [SpawnResult(status="failed", error="turn limit hit")]}
    )
    deps, store, *_ = make_deps(tmp_path, TWO_STEP)
    deps.spawn = spawn
    deps.attempts = 1

    run_once(deps)

    assert [c["attempt"] for c in spawn.calls if c["id"] == "TEAM-7.01"] == [1]
    # With retries off the error is reported bare, with no attempt count.
    assert store.load("TEAM-7").subtask("TEAM-7.01").blocked_reason == "turn limit hit"


# -- what the retry actually tells the agent ---------------------------------


def test_first_attempt_prompt_says_nothing_about_retries():
    prompt = build_subtask_prompt(
        subtask_readme="## Goal\nDo it",
        parent_ticket=Ticket(identifier="TEAM-7", title="t"),
        repo_paths=[],
        sibling_logs=[],
    )
    assert "Retry" not in prompt


def test_retry_prompt_warns_about_partial_changes_in_the_tree():
    prompt = build_subtask_prompt(
        subtask_readme="## Goal\nDo it",
        parent_ticket=Ticket(identifier="TEAM-7", title="t"),
        repo_paths=[],
        sibling_logs=[],
        attempt=2,
        previous_error="turn limit hit",
    )
    assert "Retry (attempt 2)" in prompt
    assert "turn limit hit" in prompt
    assert "partial changes" in prompt
    # The sub-task brief must still be there, and still come first in substance.
    assert "## Goal" in prompt
