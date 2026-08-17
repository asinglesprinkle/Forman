"""Ordering survives the trip from draft to Linear.

The bug these pin: `blocked_by` naming a ticket that already existed was thrown
away by a resolver that only understood indices into the current batch. Nothing
failed, nothing was logged, and the board came out with no relations on it — so
the pull phase, whose ticket-level sort reads exactly those relations, happily
worked a ticket before the one it depended on.
"""

import json

import pytest

from forman.linear_client import StubLinearClient
from forman.models import Ticket
from forman.push import (
    PushError,
    backlog_digest,
    create_tickets,
    push,
    ref_problems,
    to_tickets,
)
from forman.spawn import AgentRun


def draft(title, **fields):
    return {
        "title": title,
        "priority": "high",
        "labels": [],
        "project": None,
        "estimate": "s",
        "blocked_by": fields.get("blocked_by", []),
        "blocks": fields.get("blocks", []),
        "problem": "Something is missing.",
        "acceptance_criteria": ["It is no longer missing"],
        "context": "src/",
        "out_of_scope": "Everything else",
    }


def tickets(*drafts):
    return to_tickets(list(drafts))


def stub(existing=None):
    return StubLinearClient(tickets=existing or [], next_number=100)


# -- references to tickets that already exist --------------------------------


def test_a_blocker_that_already_exists_reaches_linear():
    """The ENG-40 case: infrastructure pushed on Monday, the work that needs it
    pushed on Tuesday. Tuesday's push has no index to point at."""
    linear = stub([Ticket(identifier="TEAM-40", title="Add the containers")])

    created = create_tickets(
        tickets(draft("Use the containers", blocked_by=["TEAM-40"])), linear
    )

    assert created[0].blocked_by == ["TEAM-40"]
    assert ("TEAM-40", "TEAM-100") in linear.relations
    assert linear.tickets["TEAM-100"].blocked_by == ["TEAM-40"]


def test_an_existing_blocker_is_normalised_to_upper_case():
    linear = stub([Ticket(identifier="TEAM-40", title="Add the containers")])

    created = create_tickets(tickets(draft("Use them", blocked_by=["team-40"])), linear)

    assert created[0].blocked_by == ["TEAM-40"]


def test_index_and_identifier_references_mix_in_one_batch():
    linear = stub([Ticket(identifier="TEAM-40", title="Containers")])

    created = create_tickets(
        tickets(
            draft("Migrations", blocked_by=["TEAM-40"]),
            draft("Routes", blocked_by=[1, "TEAM-40"]),
        ),
        linear,
    )

    by_title = {t.title: t for t in created}
    assert by_title["Migrations"].blocked_by == ["TEAM-40"]
    assert set(by_title["Routes"].blocked_by) == {
        by_title["Migrations"].identifier,
        "TEAM-40",
    }


def test_a_blocker_outside_the_batch_does_not_constrain_creation_order():
    """It was created long ago, so it imposes nothing on this batch's ordering."""
    linear = stub([Ticket(identifier="TEAM-40", title="Containers")])

    created = create_tickets(
        tickets(draft("One", blocked_by=["TEAM-40"]), draft("Two", blocked_by=[1])),
        linear,
    )

    assert [t.title for t in created] == ["One", "Two"]


# -- the blocks direction ----------------------------------------------------


def test_blocks_is_recorded_not_just_blocked_by():
    """A foundational ticket knows what it enables before those tickets exist.
    Expressing ordering that way round used to record nothing at all."""
    linear = stub()

    created = create_tickets(
        tickets(draft("Containers", blocks=[2]), draft("Routes")), linear
    )

    containers, routes = created
    assert (containers.identifier, routes.identifier) in linear.relations
    assert linear.tickets[routes.identifier].blocked_by == [containers.identifier]


def test_blocks_may_name_a_ticket_that_already_exists():
    linear = stub([Ticket(identifier="TEAM-44", title="The tests")])

    created = create_tickets(tickets(draft("Routes", blocks=["TEAM-44"])), linear)

    assert created[0].blocks == ["TEAM-44"]
    assert (created[0].identifier, "TEAM-44") in linear.relations


def test_an_edge_stated_from_both_ends_is_written_once():
    linear = stub()

    create_tickets(
        tickets(draft("First", blocks=[2]), draft("Second", blocked_by=[1])), linear
    )

    assert linear.relations.count(("TEAM-100", "TEAM-101")) == 1


# -- references that cannot mean anything ------------------------------------


@pytest.mark.parametrize("ref", ["9", "0", "the auth ticket", "TEAM-", "-42"])
def test_a_reference_that_resolves_to_nothing_is_refused(ref):
    linear = stub()

    with pytest.raises(PushError):
        create_tickets(tickets(draft("One", blocked_by=[ref])), linear)

    assert linear.tickets == {}, "nothing may be created once a ref is unusable"


def test_a_ticket_may_not_block_itself():
    assert ref_problems(tickets(draft("One", blocked_by=[1]))) != []


def test_a_good_batch_has_no_problems():
    assert ref_problems(tickets(draft("One"), draft("Two", blocked_by=[1]))) == []


# -- a refused relation is loud, not fatal -----------------------------------


class RefusesRelations(StubLinearClient):
    def relate_blocks(self, blocker: str, blocked: str) -> None:
        raise RuntimeError("Linear said no")


def test_a_refused_relation_warns_and_keeps_the_tickets():
    linear = RefusesRelations(next_number=100)
    warnings: list[str] = []

    created = create_tickets(
        tickets(draft("One"), draft("Two", blocked_by=[1])), linear, warnings.append
    )

    assert len(created) == 2, "the tickets exist; a lost edge cannot undo them"
    assert warnings and "TEAM-100" in warnings[0] and "TEAM-101" in warnings[0]


# -- the backlog the drafting agent gets to see ------------------------------


def test_the_digest_lists_open_tickets_oldest_first():
    linear = stub(
        [
            Ticket(identifier="TEAM-9", title="Nine"),
            Ticket(identifier="TEAM-40", title="Forty"),
            Ticket(identifier="TEAM-41", title="Done one", status="done"),
        ]
    )

    digest = backlog_digest(linear)

    assert digest.index("TEAM-9") < digest.index("TEAM-40"), "sorted numerically"
    assert "Done one" not in digest, "a finished ticket cannot be a blocker"
    assert "blocked_by" in digest


def test_the_digest_is_empty_when_the_board_is():
    assert backlog_digest(stub()) == ""


def test_a_backlog_that_cannot_be_fetched_does_not_stop_the_draft():
    class Broken(StubLinearClient):
        def list_assigned(self):
            raise RuntimeError("Linear is down")

    assert backlog_digest(Broken()) == ""


def test_the_existing_backlog_reaches_the_drafting_agent():
    linear = stub([Ticket(identifier="TEAM-40", title="Add the containers")])
    prompts: list[str] = []

    def runner(*, prompt, **kwargs):
        prompts.append(prompt)
        return AgentRun(text=json.dumps({"tickets": [draft("New work")]}))

    push(prose="do the thing", linear=linear, runner=runner)

    assert "TEAM-40" in prompts[0]
    assert "Add the containers" in prompts[0]


def test_a_dry_run_still_reports_a_broken_reference():
    """Otherwise the first run that actually creates anything is the one that
    finds out."""

    def runner(*, prompt, **kwargs):
        return AgentRun(
            text=json.dumps({"tickets": [draft("One", blocked_by=["nonsense"])]})
        )

    with pytest.raises(PushError):
        push(prose="x", linear=stub(), runner=runner, dry_run=True)
