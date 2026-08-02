"""The review port, and the promise that it did not change the terminal.

push_interactive grew a typed Reviewer so that something embedding Forman can
tell a question apart from an approval without matching prompt strings. The
first test here is the one that matters: the old ask/show call still produces
the same transcript it always did.
"""

from __future__ import annotations

import json

import pytest

from forman.linear_client import StubLinearClient
from forman.models import Ticket
from forman.push import Aborted, PushError, push_interactive
from forman.review import (
    Approval,
    Decision,
    Question,
    Reviewer,
    TerminalReviewer,
    parse_decision,
)
from forman.spawn import AgentRun

DRAFT = json.dumps(
    {
        "tickets": [
            {
                "title": "Add a rate limiter",
                "priority": "high",
                "labels": [],
                "project": None,
                "estimate": "m",
                "blocked_by": [],
                "blocks": [],
                "problem": "The API has no rate limiting.",
                "acceptance_criteria": ["429 after 100 requests in a minute"],
                "context": "src/api/middleware.py",
                "out_of_scope": "Per-tenant quotas.",
            }
        ]
    }
)

SECOND_DRAFT = DRAFT.replace("Add a rate limiter", "Add a token bucket rate limiter")


class ScriptedConversation:
    """Replays agent turns and records the openings it was given."""

    def __init__(self, turns: list[str]) -> None:
        self.turns = list(turns)
        self.openings: list[str] = []

    def __call__(self, *, respond, opening, **_kwargs) -> AgentRun:
        self.openings.append(opening)
        text = self.turns.pop(0)
        while True:
            reply = respond(text)
            if reply is None or not self.turns:
                return AgentRun(text=text)
            text = self.turns.pop(0)


class RecordingReviewer:
    """A Reviewer that answers from a script and remembers what it was asked."""

    def __init__(self, answers: list[str], decisions: list[Decision]) -> None:
        self.answers = list(answers)
        self.decisions = list(decisions)
        self.questions: list[Question] = []
        self.approvals: list[Approval] = []
        self.shown: list[str] = []

    def show(self, text: str) -> None:
        self.shown.append(text)

    def answer(self, question: Question) -> str:
        self.questions.append(question)
        return self.answers.pop(0)

    def decide(self, approval: Approval) -> Decision:
        self.approvals.append(approval)
        return self.decisions.pop(0)


def _transcript(turns: list[str], typed: list[str]) -> tuple[list[str], list[str]]:
    """Run the legacy ask/show path and return what was shown and asked."""
    shown: list[str] = []
    asked: list[str] = []
    answers = list(typed)

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return answers.pop(0)

    tickets = push_interactive(
        prose="rate limit the api",
        linear=StubLinearClient(),
        ask=ask,
        show=shown.append,
        conversation=ScriptedConversation(turns),
    )
    assert tickets
    return shown, asked


def test_legacy_ask_and_show_still_drive_the_same_transcript():
    shown, asked = _transcript(
        turns=["Which endpoints, and what limit?", DRAFT],
        typed=["all of them, 100/min", "c"],
    )

    # The agent's question is shown verbatim, then the drafts, and the two
    # prompts are exactly the ones the terminal has always used.
    assert shown[0] == "Which endpoints, and what limit?"
    assert shown[1] == ""
    assert "title: Add a rate limiter" in shown[2]
    assert asked == [
        "> ",
        "[c]reate 1 ticket(s), [e]dit, [q]uit, or type feedback to redraft: ",
    ]


def test_an_empty_line_at_the_gate_still_creates():
    linear = StubLinearClient()
    tickets = push_interactive(
        prose="rate limit the api",
        linear=linear,
        ask=lambda _prompt: "",
        show=lambda _text: None,
        conversation=ScriptedConversation([DRAFT]),
    )
    assert [t.identifier for t in tickets] == ["TEAM-100"]


def test_a_reviewer_replaces_ask_and_show_entirely():
    linear = StubLinearClient()
    reviewer = RecordingReviewer(
        answers=["all of them, 100/min"],
        decisions=[Decision("create")],
    )

    tickets = push_interactive(
        prose="rate limit the api",
        linear=linear,
        reviewer=reviewer,
        conversation=ScriptedConversation(["Which endpoints?", DRAFT]),
    )

    assert [t.title for t in tickets] == ["Add a rate limiter"]
    assert [q.text for q in reviewer.questions] == ["Which endpoints?"]
    assert [q.round for q in reviewer.questions] == [1]
    # The approval carries both the objects and their rendering, so an embedder
    # never has to re-render to show them.
    assert len(reviewer.approvals) == 1
    assert reviewer.approvals[0].tickets[0].title == "Add a rate limiter"
    assert "title: Add a rate limiter" in reviewer.approvals[0].rendered


def test_quit_creates_nothing():
    linear = StubLinearClient()
    reviewer = RecordingReviewer(answers=[], decisions=[Decision("quit")])

    with pytest.raises(Aborted):
        push_interactive(
            prose="rate limit the api",
            linear=linear,
            reviewer=reviewer,
            conversation=ScriptedConversation([DRAFT]),
        )

    assert linear.tickets == {}


def test_feedback_redrafts_without_creating_then_creates():
    linear = StubLinearClient()
    reviewer = RecordingReviewer(
        answers=[],
        decisions=[Decision("feedback", "use a token bucket"), Decision("create")],
    )
    conversation = ScriptedConversation([DRAFT, SECOND_DRAFT])

    tickets = push_interactive(
        prose="rate limit the api",
        linear=linear,
        reviewer=reviewer,
        conversation=conversation,
    )

    assert [t.title for t in tickets] == ["Add a token bucket rate limiter"]
    assert "use a token bucket" in conversation.openings[1]
    # Two gates, one creation: the redraft did not file anything.
    assert len(reviewer.approvals) == 2
    assert len(linear.tickets) == 1


def test_edit_without_an_editor_says_so_and_asks_again():
    linear = StubLinearClient()
    reviewer = RecordingReviewer(
        answers=[], decisions=[Decision("edit"), Decision("quit")]
    )

    with pytest.raises(Aborted):
        push_interactive(
            prose="rate limit the api",
            linear=linear,
            reviewer=reviewer,
            conversation=ScriptedConversation([DRAFT]),
        )

    assert reviewer.shown == ["No editor available. Type feedback instead."]


def test_an_unknown_action_is_refused_rather_than_guessed():
    reviewer = RecordingReviewer(answers=[], decisions=[Decision("merge")])

    with pytest.raises(PushError, match="unknown action"):
        push_interactive(
            prose="rate limit the api",
            linear=StubLinearClient(),
            reviewer=reviewer,
            conversation=ScriptedConversation([DRAFT]),
        )


def test_push_interactive_refuses_a_half_configured_call():
    with pytest.raises(TypeError, match="either reviewer"):
        push_interactive(
            prose="rate limit the api",
            linear=StubLinearClient(),
            show=lambda _text: None,
            conversation=ScriptedConversation([DRAFT]),
        )


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("", "create"),
        ("  ", "create"),
        ("c", "create"),
        ("CREATE", "create"),
        ("yes", "create"),
        ("q", "quit"),
        ("No", "quit"),
        ("e", "edit"),
        ("EDIT", "edit"),
        ("make it two tickets", "feedback"),
    ],
)
def test_parse_decision(typed, expected):
    assert parse_decision(typed).action == expected


def test_parse_decision_keeps_the_feedback_verbatim():
    decision = parse_decision("  split the auth part out  ")
    assert decision == Decision("feedback", "split the auth part out")


def test_terminal_reviewer_satisfies_the_protocol():
    assert isinstance(TerminalReviewer(), Reviewer)
    assert isinstance(RecordingReviewer([], []), Reviewer)


def _recorder(asked: list[str], reply: str):
    def ask(prompt: str) -> str:
        asked.append(prompt)
        return reply

    return ask


def test_terminal_reviewer_shows_the_question_before_prompting():
    asked: list[str] = []
    shown: list[str] = []
    reviewer = TerminalReviewer(ask=_recorder(asked, "because"), show=shown.append)

    assert reviewer.answer(Question(text="why?", round=2)) == "because"
    assert shown == ["why?"]
    assert asked == ["> "]


def test_terminal_reviewer_counts_the_tickets_in_its_prompt():
    asked: list[str] = []
    reviewer = TerminalReviewer(ask=_recorder(asked, "q"), show=lambda _text: None)
    approval = Approval(tickets=[Ticket("", "a"), Ticket("", "b")], rendered="...")

    assert reviewer.decide(approval) == Decision("quit")
    assert asked == ["[c]reate 2 ticket(s), [e]dit, [q]uit, or type feedback to redraft: "]
