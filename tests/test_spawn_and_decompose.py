"""The pure logic inside the activity layers: parsing agent output, numbering
sub-tasks, naming branches. No SDK, no git, no network."""

import pytest

from forman.decompose import (
    DecompositionError,
    order_and_number,
    parse_decomposition,
    render_subtask_readme,
)
from forman.git_ops import branch_name, slugify
from forman.models import SubTaskSpec, SubTaskStatus, Ticket
from forman.spawn import AgentRun, extract_last_json, result_from_run, spawn_agent


# -- reading what the agent said ---------------------------------------------


def test_extract_json_from_a_bare_object():
    assert extract_last_json('{"status": "done"}') == {"status": "done"}


def test_extract_json_ignores_surrounding_prose_and_fences():
    text = (
        "I finished the work.\n\n```json\n"
        '{"status": "done", "summary": "added the helper", "blocked_reason": null}\n'
        "```\n"
    )
    assert extract_last_json(text)["summary"] == "added the helper"


def test_extract_json_takes_the_last_contract_shaped_object():
    text = '{"status": "thinking out loud"} then later {"status": "done", "summary": "ok"}'
    assert extract_last_json(text)["summary"] == "ok"


def test_extract_json_skips_objects_without_a_status():
    text = '{"note": "some tool output"}\n{"status": "blocked", "blocked_reason": "no key"}'
    assert extract_last_json(text)["status"] == "blocked"


def test_extract_json_returns_none_when_there_is_none():
    assert extract_last_json("I could not do it, sorry.") is None


# -- turning a session into an outcome ---------------------------------------


def test_done_result():
    run = AgentRun(text='{"status": "done", "summary": "wrote the helper"}', session_id="s1")
    result = result_from_run(run)
    assert result.ok and result.summary == "wrote the helper"
    assert result.session_id == "s1"


def test_blocked_is_a_business_outcome_not_an_error():
    run = AgentRun(text='{"status": "blocked", "blocked_reason": "needs a prod credential"}')
    result = result_from_run(run)
    assert result.status == SubTaskStatus.BLOCKED.value
    assert result.blocked_reason == "needs a prod credential"
    assert result.error is None


def test_blocked_without_a_reason_still_records_something():
    result = result_from_run(AgentRun(text='{"status": "blocked"}'))
    assert result.blocked_reason


def test_sdk_error_is_failed_not_blocked():
    result = result_from_run(AgentRun(error="ConnectionError: dropped"))
    assert result.status == SubTaskStatus.FAILED.value
    assert "ConnectionError" in result.error


def test_turn_limit_is_failed():
    result = result_from_run(AgentRun(text='{"status": "done"}', turn_limit_hit=True))
    assert result.status == SubTaskStatus.FAILED.value
    assert "turn limit" in result.error


def test_missing_json_is_failed_and_keeps_the_raw_text():
    result = result_from_run(AgentRun(text="I think I am done?"))
    assert result.status == SubTaskStatus.FAILED.value
    assert result.raw == "I think I am done?"


def test_unrecognized_status_is_failed():
    result = result_from_run(AgentRun(text='{"status": "mostly done"}'))
    assert result.status == SubTaskStatus.FAILED.value
    assert "mostly done" in result.error


def test_spawn_agent_passes_the_contract_and_the_context(tmp_path):
    captured = {}

    def fake_runner(**kwargs):
        captured.update(kwargs)
        return AgentRun(text='{"status": "done", "summary": "ok"}')

    ticket = Ticket(identifier="TEAM-7", title="Do the thing", description="Because reasons.")
    result = spawn_agent(
        subtask_readme="## Goal\nAdd a helper",
        parent_ticket=ticket,
        repo_paths=["/repo"],
        sibling_logs=[("TEAM-7.01", "did the earlier bit")],
        cwd=tmp_path,
        runner=fake_runner,
    )

    assert result.ok
    assert "append" in captured["system_prompt"]  # the SPAWN CONTRACT
    prompt = captured["prompt"]
    assert "Add a helper" in prompt
    assert "TEAM-7: Do the thing" in prompt
    assert "did the earlier bit" in prompt
    assert captured["cwd"] == tmp_path
    assert captured["allowed_tools"] == ["Bash", "Edit", "Read", "Grep"]


# -- decomposition -----------------------------------------------------------


def test_parse_decomposition_reads_goals_and_deps():
    specs = parse_decomposition(
        '{"subtasks": [{"goal": "one"}, {"goal": "two", "depends_on": [1]}]}'
    )
    assert [s.goal for s in specs] == ["one", "two"]
    assert specs[1].depends_on == ["1"]


def test_parse_decomposition_rejects_empty_and_goalless():
    with pytest.raises(DecompositionError):
        parse_decomposition('{"subtasks": []}')
    with pytest.raises(DecompositionError):
        parse_decomposition('{"subtasks": [{"goal": "  "}]}')
    with pytest.raises(DecompositionError):
        parse_decomposition("no json here")


def test_numbering_follows_dependency_order():
    specs = [
        SubTaskSpec(goal="second", depends_on=["2"]),
        SubTaskSpec(goal="first"),
    ]
    pairs = order_and_number("TEAM-7", specs)
    assert [st.id for st, _ in pairs] == ["TEAM-7.01", "TEAM-7.02"]
    assert [st.goal for st, _ in pairs] == ["first", "second"]
    # .01 can never depend on .02
    assert pairs[0][0].depends_on == []
    assert pairs[1][0].depends_on == ["TEAM-7.01"]


def test_dependency_refs_accept_subtask_ids_too():
    specs = [SubTaskSpec(goal="a"), SubTaskSpec(goal="b", depends_on=["TEAM-7.01"])]
    pairs = order_and_number("TEAM-7", specs)
    assert pairs[1][0].depends_on == ["TEAM-7.01"]


def test_unresolvable_dependency_is_dropped_rather_than_failing_the_run():
    specs = [SubTaskSpec(goal="a", depends_on=["47", "some-other-ticket"])]
    pairs = order_and_number("TEAM-7", specs)
    assert pairs[0][0].depends_on == []


def test_self_dependency_is_dropped():
    specs = [SubTaskSpec(goal="a", depends_on=["1"])]
    assert order_and_number("TEAM-7", specs)[0][0].depends_on == []


def test_circular_dependencies_fail_the_decomposition():
    specs = [SubTaskSpec(goal="a", depends_on=["2"]), SubTaskSpec(goal="b", depends_on=["1"])]
    with pytest.raises(DecompositionError, match="circular"):
        order_and_number("TEAM-7", specs)


def test_readme_omits_optional_sections_when_empty():
    specs = [SubTaskSpec(goal="just do it")]
    subtask, spec = order_and_number("TEAM-7", specs)[0]
    body = render_subtask_readme(subtask, Ticket(identifier="TEAM-7", title="t"), spec)

    assert "## Goal" in body and "## Definition of done" in body
    assert "## Test plan" not in body
    assert "## Likely files" not in body
    assert "## Notes for executor" not in body
    assert body.rstrip().endswith("<!-- spawn appends below this line; never edits above it -->")


def test_readme_includes_optional_sections_when_real():
    specs = [
        SubTaskSpec(
            goal="do it",
            definition_of_done=["tests pass"],
            files=["src/a.py"],
            test_plan="run pytest",
            notes="watch out for the cache",
        )
    ]
    subtask, spec = order_and_number("TEAM-7", specs)[0]
    body = render_subtask_readme(subtask, Ticket(identifier="TEAM-7", title="t"), spec)

    assert "- [ ] tests pass" in body
    assert "src/a.py" in body
    assert "run pytest" in body
    assert "watch out for the cache" in body


# -- branch naming -----------------------------------------------------------


def test_branch_name_derives_from_the_ticket():
    assert (
        branch_name("TEAM-7", "Add rate limiting to the auth endpoint")
        == "team-7/add-rate-limiting-to-the-auth-endpoint"
    )


def test_branch_name_works_for_any_prefix():
    assert branch_name("ABC-142", "Fix token refresh") == "abc-142/fix-token-refresh"


def test_slug_strips_punctuation_and_collapses_separators():
    assert slugify("Fix the (broken!) OAuth   flow -- again") == "fix-the-broken-oauth-flow-again"


def test_slug_truncates_without_ending_mid_word():
    slug = slugify("a" * 20 + " " + "b" * 20 + " " + "c" * 20)
    assert len(slug) <= 50
    assert not slug.endswith("-")
    assert "c" not in slug  # the partial trailing word was dropped


def test_slug_never_returns_empty():
    assert slugify("!!!") == "work"
