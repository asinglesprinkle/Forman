"""state.json round-trips, and manifest.md renders from it."""

import json

from foreman.models import SubTask, SubTaskStatus, TicketState
from foreman.state import (
    StateStore,
    next_ready_subtask,
    render_manifest,
    state_from_dict,
    state_to_dict,
)


def make_state() -> TicketState:
    return TicketState(
        ticket="TEAM-7",
        title="Add rate limiting to the auth endpoint",
        status="in_progress",
        branch="team-7/add-rate-limiting-to-the-auth-endpoint",
        pulled_at="2026-08-01T10:00:00+00:00",
        subtasks=[
            SubTask(id="TEAM-7.01", goal="Add a token bucket helper"),
            SubTask(id="TEAM-7.02", goal="Wire it into the auth route", depends_on=["TEAM-7.01"]),
        ],
    )


def test_round_trip_through_dict():
    state = make_state()
    assert state_from_dict(state_to_dict(state)) == state


def test_round_trip_carries_session_id_and_cost(tmp_path):
    state = make_state()
    state.subtask("TEAM-7.01").session_id = "sess-abc123"
    state.subtask("TEAM-7.01").cost_usd = 0.4213

    as_dict = state_to_dict(state)
    assert as_dict["subtasks"][0]["session_id"] == "sess-abc123"
    assert as_dict["subtasks"][0]["cost_usd"] == 0.4213
    assert state_from_dict(as_dict) == state

    # And through the file, not just the dicts.
    store = StateStore(tmp_path)
    store.save(state)
    assert store.load("TEAM-7") == state


def test_state_file_without_the_cost_fields_still_loads(tmp_path):
    # A state.json written before session_id/cost_usd existed. state_from_dict
    # does SubTask(**st), so the missing keys fall back to the dataclass
    # defaults rather than raising.
    legacy = {
        "ticket": "TEAM-7",
        "title": "Add rate limiting to the auth endpoint",
        "status": "in_progress",
        "branch": "team-7/add-rate-limiting-to-the-auth-endpoint",
        "pulled_at": "2026-08-01T10:00:00+00:00",
        "pr_url": None,
        "subtasks": [
            {
                "id": "TEAM-7.01",
                "goal": "Add a token bucket helper",
                "status": "done",
                "depends_on": [],
                "blocked_reason": None,
                "log": None,
                "started_at": "2026-08-01T10:01:00+00:00",
                "finished_at": "2026-08-01T10:09:00+00:00",
            }
        ],
    }
    store = StateStore(tmp_path)
    store.init("TEAM-7")
    store.state_path("TEAM-7").write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    loaded = store.load("TEAM-7")
    st = loaded.subtask("TEAM-7.01")
    assert st.session_id is None
    assert st.cost_usd is None
    assert st.finished_at == "2026-08-01T10:09:00+00:00"
    assert loaded.all_done()

    # Saving it back adds the new keys without disturbing anything else.
    store.save(loaded)
    written = json.loads(store.state_path("TEAM-7").read_text(encoding="utf-8"))
    assert written["subtasks"][0]["session_id"] is None
    assert written["subtasks"][0]["cost_usd"] is None
    assert store.load("TEAM-7") == loaded


def test_save_writes_state_and_renders_manifest(tmp_path):
    store = StateStore(tmp_path)
    store.save(make_state())

    assert store.state_path("TEAM-7").is_file()
    assert store.manifest_path("TEAM-7").is_file()
    assert store.load("TEAM-7") == make_state()
    assert store.tickets() == ["TEAM-7"]


def test_manifest_is_rewritten_on_every_save(tmp_path):
    store = StateStore(tmp_path)
    state = make_state()
    store.save(state)
    assert "[ ] `TEAM-7.01`" in store.manifest_path("TEAM-7").read_text()

    state.subtask("TEAM-7.01").status = SubTaskStatus.DONE.value
    store.save(state)
    manifest = store.manifest_path("TEAM-7").read_text()
    assert "[x] `TEAM-7.01`" in manifest
    # And the dependent line stops saying it is waiting.
    assert "waiting on" not in manifest


def test_manifest_markers_and_notes():
    state = make_state()
    state.subtasks = [
        SubTask(id="A.01", goal="done one", status=SubTaskStatus.DONE.value),
        SubTask(id="A.02", goal="running one", status=SubTaskStatus.IN_PROGRESS.value),
        SubTask(id="A.03", goal="waiting one", depends_on=["A.02"]),
        SubTask(
            id="A.04",
            goal="blocked one",
            status=SubTaskStatus.BLOCKED.value,
            blocked_reason="needs a STRIPE_KEY only the human has",
        ),
        SubTask(
            id="A.05",
            goal="failed one",
            status=SubTaskStatus.FAILED.value,
            blocked_reason="turn limit hit",
        ),
    ]
    out = render_manifest(state)

    assert "[x] `A.01`" in out
    assert "[~] `A.02`" in out and "(in progress)" in out
    assert "[ ] `A.03`" in out and "(waiting on A.02)" in out
    assert "[!] `A.04`" in out and "needs a STRIPE_KEY" in out
    assert "[X] `A.05`" in out and "turn limit hit" in out


def test_next_ready_subtask_respects_depends_on():
    state = make_state()
    assert next_ready_subtask(state).id == "TEAM-7.01"

    state.subtask("TEAM-7.01").status = SubTaskStatus.DONE.value
    assert next_ready_subtask(state).id == "TEAM-7.02"

    state.subtask("TEAM-7.02").status = SubTaskStatus.DONE.value
    assert next_ready_subtask(state) is None
    assert state.all_done()


def test_blocked_dependency_is_terminal_for_the_run():
    # A blocked sub-task never becomes done, so its dependents can never run.
    # next_ready_subtask returns None and the orchestrator breaks: no retry pass.
    state = make_state()
    state.subtask("TEAM-7.01").status = SubTaskStatus.BLOCKED.value
    state.subtask("TEAM-7.01").blocked_reason = "waiting on an upstream ticket"
    assert next_ready_subtask(state) is None
    assert not state.all_done()


def test_append_execution_log_never_touches_the_brief(tmp_path):
    store = StateStore(tmp_path)
    store.write_subtask_readme(
        "TEAM-7",
        "TEAM-7.01",
        "## Goal\nDo the thing\n\n---\n## Execution log\n<!-- appended below -->\n",
    )
    store.append_execution_log("TEAM-7", "TEAM-7.01", "Did the thing.")
    store.append_execution_log("TEAM-7", "TEAM-7.01", "Then did another thing.")

    body = store.read_subtask_readme("TEAM-7", "TEAM-7.01")
    assert body.index("## Goal") < body.index("## Execution log")
    assert body.index("Did the thing.") < body.index("Then did another thing.")
    assert body.count("## Goal") == 1
