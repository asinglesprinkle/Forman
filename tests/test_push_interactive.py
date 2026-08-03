"""The conversational push, with every side of the conversation injected.

`pull` has always stopped at a human gate. Until this existed `push` did not:
one line of prose became real tickets in a real workspace, unseen. These tests
pin that nothing is created until a human says so.
"""

import pytest

from forman.linear_client import StubLinearClient
from forman.models import Ticket
from forman.push import (
    Aborted,
    PushError,
    parse_drafts,
    parse_ticket_markdown,
    push_interactive,
    render_drafts,
    render_ticket_markdown,
    to_tickets,
)
from forman.spawn import AgentRun

DRAFT_JSON = """
Here is the ticket.
{"tickets": [{
  "title": "Refresh auth tokens before they expire mid-request",
  "priority": "high",
  "labels": ["auth"],
  "project": null,
  "estimate": "s",
  "blocked_by": [],
  "blocks": [],
  "problem": "Long requests outlive their token and 401 halfway through.",
  "acceptance_criteria": ["A request that outlives its token completes"],
  "context": "src/auth/client.py",
  "out_of_scope": "The mobile client"
}]}
"""

QUESTION = "Two things: is this the refresh path specifically, and is mobile in scope?"


class ScriptedConversation:
    """Replays agent turns and records what the human was asked."""

    def __init__(self, turns: list[str]):
        self.turns = list(turns)
        self.openings: list[str] = []

    def __call__(self, *, system_prompt, opening, respond, **kwargs):
        self.openings.append(opening)
        text = ""
        for text in self.turns:
            if respond(text) is None:
                break
        return AgentRun(text=text)


def make(answers, conversation=None, edit=None):
    """Wire up push_interactive with canned human answers."""
    asked: list[str] = []
    shown: list[str] = []
    replies = list(answers)

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return replies.pop(0) if replies else "q"

    linear = StubLinearClient()
    kwargs = {
        "prose": "the auth client keeps dropping sessions",
        "linear": linear,
        "ask": ask,
        "show": shown.append,
        "conversation": conversation or ScriptedConversation([DRAFT_JSON]),
        "edit": edit,
    }
    return kwargs, linear, asked, shown


# -- the gate ----------------------------------------------------------------


def test_nothing_is_created_until_the_human_says_so():
    kwargs, linear, _asked, _shown = make(["q"])

    with pytest.raises(Aborted):
        push_interactive(**kwargs)

    assert linear.tickets == {}


def test_creating_files_the_ticket():
    kwargs, linear, _asked, _shown = make(["c"])
    made = push_interactive(**kwargs)

    assert len(made) == 1
    assert made[0].title == "Refresh auth tokens before they expire mid-request"
    assert list(linear.tickets) == [made[0].identifier]


def test_bare_enter_accepts_the_draft():
    kwargs, _linear, *_ = make([""])
    assert len(push_interactive(**kwargs)) == 1


def test_the_draft_is_shown_before_anything_is_created():
    kwargs, _linear, _asked, shown = make(["c"])
    push_interactive(**kwargs)

    body = "\n".join(shown)
    assert "Refresh auth tokens" in body
    assert "## Acceptance criteria" in body


# -- questions ---------------------------------------------------------------


def test_questions_reach_the_human_and_answers_reach_the_agent():
    convo = ScriptedConversation([QUESTION, DRAFT_JSON])
    kwargs, linear, _asked, shown = make(["yes, refresh only, web", "c"], convo)

    push_interactive(**kwargs)

    assert QUESTION in shown  # the human saw the question
    assert linear.tickets  # and it still ended in a created ticket


def test_json_ends_the_questioning():
    # The draft arrives first, so the human is never asked anything except
    # whether to create it.
    convo = ScriptedConversation([DRAFT_JSON, QUESTION])
    kwargs, _linear, asked, shown = make(["c"], convo)

    push_interactive(**kwargs)

    assert QUESTION not in shown
    assert len(asked) == 1


def test_endless_questions_are_cut_off():
    # An agent that will not stop asking. The human answers three times and
    # then says create; nothing beyond the cap is ever put to them.
    convo = ScriptedConversation([QUESTION] * 10 + [DRAFT_JSON])
    kwargs, linear, _asked, shown = make(["a", "b", "c", "c"], convo)

    push_interactive(**kwargs)

    assert shown.count(QUESTION) == 3  # MAX_QUESTION_ROUNDS, not 10
    assert linear.tickets


# -- feedback and editing ----------------------------------------------------


def test_feedback_redrafts_without_creating():
    convo = ScriptedConversation([DRAFT_JSON])
    kwargs, linear, _asked, _shown = make(["make the title shorter", "c"], convo)

    push_interactive(**kwargs)

    # Two conversations: the original, then a redraft carrying the feedback.
    assert len(convo.openings) == 2
    assert "make the title shorter" in convo.openings[1]
    assert len(linear.tickets) == 1  # created once, at the end


def test_edit_replaces_the_draft_with_whatever_came_back():
    def fake_editor(text: str) -> str:
        return text.replace(
            "Refresh auth tokens before they expire mid-request",
            "Fix token refresh",
        )

    kwargs, _linear, _asked, _shown = make(["e", "c"], edit=fake_editor)
    made = push_interactive(**kwargs)

    assert made[0].title == "Fix token refresh"


def test_a_broken_edit_does_not_destroy_the_draft():
    kwargs, _linear, _asked, shown = make(["e", "c"], edit=lambda _text: "garbage")
    made = push_interactive(**kwargs)

    assert "Could not read that back" in "\n".join(shown)
    assert made[0].title == "Refresh auth tokens before they expire mid-request"


def test_edit_without_an_editor_says_so_instead_of_crashing():
    kwargs, _linear, _asked, shown = make(["e", "c"], edit=None)
    push_interactive(**kwargs)
    assert "No editor available" in "\n".join(shown)


# -- round-tripping ----------------------------------------------------------


def test_a_rendered_ticket_parses_back_to_the_same_thing():
    ticket = to_tickets(
        [
            {
                "title": "Do the thing",
                "priority": "urgent",
                "labels": ["auth", "bug"],
                "project": "Platform",
                "estimate": "m",
                "problem": "It is broken.",
                "acceptance_criteria": ["It works"],
                "context": "src/a.py",
                "out_of_scope": "everything else",
            }
        ]
    )[0]

    back = parse_ticket_markdown(render_ticket_markdown(ticket))

    assert back.title == ticket.title
    assert back.priority == "urgent"
    assert back.labels == ["auth", "bug"]
    assert back.project == "Platform"
    assert back.estimate == "m"
    assert back.description.strip() == ticket.description.strip()


def test_null_fields_survive_the_round_trip():
    ticket = Ticket(identifier="", title="t", description="body")
    back = parse_ticket_markdown(render_ticket_markdown(ticket))
    assert back.project is None and back.estimate is None and back.labels == []


def test_several_tickets_round_trip_through_one_buffer():
    tickets = [
        Ticket(identifier="", title="First", description="one"),
        Ticket(identifier="", title="Second", description="two"),
    ]
    back = parse_drafts(render_drafts(tickets))
    assert [t.title for t in back] == ["First", "Second"]


def test_an_edit_that_removes_the_title_is_rejected():
    with pytest.raises(PushError, match="no title"):
        parse_ticket_markdown("---\npriority: high\n---\n\nbody")


def test_hand_written_frontmatter_is_read_forgivingly():
    back = parse_ticket_markdown(
        "---\n"
        "title:   Spaces everywhere   \n"
        "priority: HIGH\n"
        "labels: auth, bug\n"  # no brackets
        "project: NONE\n"
        "---\n\n"
        "the body\n"
    )
    assert back.title == "Spaces everywhere"
    assert back.priority == "high"
    assert back.labels == ["auth", "bug"]
    assert back.project is None
