"""The provenance label: which tickets `forman pull` is willing to start.

The scenario these exist for is mundane and expensive: someone files twenty
tickets on you while a run is going, and the next `forman pull` picks one up and
writes code against it unsupervised. So selection is narrowed to tickets Forman
itself created, which it marks on the way out.

Deliberately not a wall. Adding the label by hand opts a ticket in, `--ticket`
works anything by name, and `--any` turns the filter off for a run. What it
stops is work starting *by default* on something nobody pointed at.
"""

from test_orchestrator_stub import TWO_STEP, make_deps

from forman.config import DEFAULT_LABEL, load_settings, parse_env
from forman.linear_client import StubLinearClient
from forman.models import Ticket
from forman.orchestrator import run_once, select_ticket, unmarked

LABEL = DEFAULT_LABEL


def marked(identifier: str, title: str = "forman's own", **kwargs) -> Ticket:
    return Ticket(identifier=identifier, title=title, labels=[LABEL], **kwargs)


def foreign(identifier: str, title: str = "filed by someone else", **kwargs) -> Ticket:
    return Ticket(identifier=identifier, title=title, **kwargs)


# -- matching ----------------------------------------------------------------


def test_the_label_matches_regardless_of_case():
    # People type labels by hand. "Forman" and "forman" are the same intent.
    assert Ticket(identifier="TEAM-1", title="x", labels=["Forman"]).has_label("forman")
    assert Ticket(identifier="TEAM-1", title="x", labels=[" forman "]).has_label(
        "forman"
    )
    assert not Ticket(identifier="TEAM-1", title="x", labels=["formant"]).has_label(
        "forman"
    )


# -- selection ---------------------------------------------------------------


def test_an_unlabelled_ticket_is_never_selected_on_its_own():
    tickets = [foreign("TEAM-1", priority="urgent"), marked("TEAM-2", priority="low")]
    # Urgent would have won on priority. Provenance comes first.
    assert select_ticket(tickets, label=LABEL).identifier == "TEAM-2"


def test_without_a_label_the_old_behaviour_is_unchanged():
    tickets = [foreign("TEAM-1", priority="urgent"), marked("TEAM-2", priority="low")]
    assert select_ticket(tickets).identifier == "TEAM-1"


def test_a_backlog_of_foreign_tickets_selects_nothing():
    tickets = [foreign(f"TEAM-{n}") for n in range(1, 21)]
    assert select_ticket(tickets, label=LABEL) is None


def test_an_unlabelled_blocker_still_blocks_a_labelled_ticket():
    """The filter narrows what we work, not what counts as a dependency.

    Filtering before the readiness check would have dropped TEAM-1 out of the
    graph, and a dependency that is not in the graph reads as a satisfied one.
    That would have started the blocked ticket, which is worse than not
    starting anything.
    """
    tickets = [marked("TEAM-2", blocked_by=["TEAM-1"]), foreign("TEAM-1")]
    assert select_ticket(tickets, label=LABEL) is None


def test_the_labelled_ticket_unblocks_once_the_foreign_blocker_is_done():
    tickets = [
        marked("TEAM-2", blocked_by=["TEAM-1"]),
        foreign("TEAM-1", status="done"),
    ]
    assert select_ticket(tickets, label=LABEL).identifier == "TEAM-2"


def test_unmarked_lists_only_workable_tickets():
    tickets = [
        marked("TEAM-1"),
        foreign("TEAM-2"),
        foreign("TEAM-3", status="done"),
        foreign("TEAM-4", status="in_review"),
    ]
    # Finished work and work at the gate were never candidates, so naming them
    # as "skipped for the label" would be a lie.
    assert [t.identifier for t in unmarked(tickets, LABEL)] == ["TEAM-2"]


# -- through the loop --------------------------------------------------------


def test_pull_leaves_a_foreign_backlog_alone(tmp_path):
    tickets = [foreign(f"TEAM-{n}") for n in range(1, 21)]
    deps, store, _linear, git = make_deps(
        tmp_path, TWO_STEP, tickets=tickets, label=LABEL
    )

    report = run_once(deps)

    assert report.outcome == "no_work"
    assert git.commits == []
    assert store.tickets() == []


def test_the_no_work_message_says_the_label_is_why(tmp_path):
    """A filter that silently finds nothing is indistinguishable from an empty
    backlog, and that is an afternoon of someone's life."""
    tickets = [foreign(f"TEAM-{n}") for n in range(1, 21)]
    deps, *_ = make_deps(tmp_path, TWO_STEP, tickets=tickets, label=LABEL)

    detail = run_once(deps).detail

    assert LABEL in detail
    assert "Skipped 20" in detail
    assert "TEAM-1" in detail  # names some of them
    assert "and 15 more" in detail
    assert "--any" in detail


def test_pull_works_the_labelled_ticket_out_of_a_mixed_backlog(tmp_path):
    tickets = [
        foreign("TEAM-1", priority="urgent"),
        foreign("TEAM-2", priority="urgent"),
        marked("TEAM-7", "Add rate limiting to the auth endpoint"),
    ]
    deps, store, *_ = make_deps(tmp_path, TWO_STEP, tickets=tickets, label=LABEL)

    report = run_once(deps)

    assert report.ticket == "TEAM-7"
    assert store.tickets() == ["TEAM-7"]


def test_naming_a_ticket_beats_the_label_and_says_so(tmp_path):
    """The escape hatch, and it is not a silent one."""
    tickets = [foreign("TEAM-31", "Somebody else's ticket")]
    goals = [("do the thing", []), ("do the other thing", ["TEAM-31.01"])]
    deps, store, *_ = make_deps(tmp_path, goals, tickets=tickets, label=LABEL)

    report = run_once(deps, ticket_id="TEAM-31")

    assert report.outcome == "in_review"
    assert store.exists("TEAM-31")
    assert any(
        LABEL in note and "asked for it by name" in note for note in report.notes
    )


def test_a_labelled_ticket_named_explicitly_gets_no_note(tmp_path):
    deps, _store, *_ = make_deps(
        tmp_path,
        TWO_STEP,
        tickets=[marked("TEAM-7", "Add rate limiting to the auth endpoint")],
        label=LABEL,
    )

    report = run_once(deps, ticket_id="TEAM-7")

    assert not [n for n in report.notes if LABEL in n]


# -- stamping on the way out -------------------------------------------------


def test_the_stub_backend_stamps_what_it_creates():
    linear = StubLinearClient(label=LABEL)
    made = linear.create(Ticket(identifier="", title="New thing"))
    assert made.has_label(LABEL)


def test_stamping_does_not_duplicate_an_existing_mark():
    linear = StubLinearClient(label=LABEL)
    made = linear.create(Ticket(identifier="", title="New thing", labels=[LABEL]))
    assert made.labels == [LABEL]


def test_a_pushed_ticket_is_immediately_pullable():
    """Push and pull have to agree, or `forman push` files work that `forman
    pull` will never touch again."""
    linear = StubLinearClient(label=LABEL)
    linear.create(Ticket(identifier="", title="New thing"))
    assert select_ticket(linear.list_assigned(), label=LABEL) is not None


# -- configuration -----------------------------------------------------------


def test_the_label_defaults_without_any_configuration():
    assert load_settings(None).label == DEFAULT_LABEL


def test_the_label_name_is_overridable():
    assert parse_env("FORMAN_LABEL=robot")["FORMAN_LABEL"] == "robot"


def test_an_empty_label_setting_falls_back_rather_than_disabling_the_filter(
    tmp_path, monkeypatch
):
    # Turning the filter off is a per-run decision (`--any`), not something a
    # blank line in a stale .env gets to make for you.
    monkeypatch.setenv("FORMAN_LABEL", "")
    (tmp_path / ".env").write_text("FORMAN_LABEL=\n", encoding="utf-8")
    assert load_settings(tmp_path).label == DEFAULT_LABEL
