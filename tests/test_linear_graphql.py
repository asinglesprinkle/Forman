"""The GraphQL Linear client, against a fake transport. No network.

These pin the mapping between Linear's shapes and ours: priority ints, workflow
states, and the blocks/blocked-by relation direction, which is the one that
would quietly corrupt the ticket-level topo-sort if it were backwards.
"""

import pytest

from forman.config import load_settings, parse_env
from forman.linear_graphql import (
    PAGE_SIZE,
    GraphQLLinearClient,
    LinearApiError,
    _status_from_state,
    _ticket_from_node,
    split_identifier,
)
from forman.models import Ticket


class FakeTransport:
    """Replays canned responses and records what was asked."""

    def __init__(self, responses: list[dict] | dict):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, query: str, variables: dict) -> dict:
        self.calls.append((query, variables))
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def issue_node(
    identifier="TEAM-7",
    title="Add rate limiting",
    priority=2,
    state=("Todo", "unstarted"),
    blocks=(),
    blocked_by=(),
):
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": title,
        "description": "Because reasons.",
        "url": f"https://linear.app/x/issue/{identifier}",
        "priority": priority,
        "estimate": None,
        "state": {"name": state[0], "type": state[1]},
        "labels": {"nodes": [{"name": "backend"}]},
        "project": {"name": "Platform"},
        "team": {"id": "uuid-team", "key": identifier.rsplit("-", 1)[0]},
        "relations": {
            "nodes": [
                {"type": "blocks", "relatedIssue": {"identifier": b}} for b in blocks
            ]
        },
        "inverseRelations": {
            "nodes": [
                {"type": "blocks", "issue": {"identifier": b}} for b in blocked_by
            ]
        },
    }


def assigned_response(nodes):
    return {
        "data": {
            "viewer": {
                "id": "u1",
                "name": "Test User",
                "assignedIssues": {"nodes": nodes},
            }
        }
    }


def client(responses, **kwargs):
    return GraphQLLinearClient(
        api_key="lin_api_test", transport=FakeTransport(responses), **kwargs
    )


# -- field mapping -----------------------------------------------------------


def test_priority_int_maps_to_our_vocabulary():
    assert _ticket_from_node(issue_node(priority=1)).priority == "urgent"
    assert _ticket_from_node(issue_node(priority=2)).priority == "high"
    assert _ticket_from_node(issue_node(priority=3)).priority == "medium"
    assert _ticket_from_node(issue_node(priority=4)).priority == "low"
    # 0 is Linear's "no priority": it must not outrank an explicit medium.
    assert _ticket_from_node(issue_node(priority=0)).priority == "low"


def test_relation_direction_is_not_backwards():
    # TEAM-7 is blocked by TEAM-5, and blocks TEAM-9.
    node = issue_node(blocked_by=["TEAM-5"], blocks=["TEAM-9"])
    ticket = _ticket_from_node(node)
    assert ticket.blocked_by == ["TEAM-5"]
    assert ticket.blocks == ["TEAM-9"]


def test_non_blocking_relations_are_ignored():
    node = issue_node()
    node["relations"]["nodes"].append(
        {"type": "related", "relatedIssue": {"identifier": "TEAM-4"}}
    )
    assert _ticket_from_node(node).blocks == []


def test_labels_and_project_come_through():
    ticket = _ticket_from_node(issue_node())
    assert ticket.labels == ["backend"]
    assert ticket.project == "Platform"


# -- workflow state mapping --------------------------------------------------


@pytest.mark.parametrize(
    "name,type_,expected",
    [
        ("Done", "completed", "done"),
        ("Cancelled", "canceled", "done"),
        ("In Review", "started", "in_review"),
        ("Code Review", "started", "in_review"),
        ("In Progress", "started", "in_progress"),
        ("Todo", "unstarted", "todo"),
        ("Backlog", "backlog", "todo"),
        ("Triage", "triage", "todo"),
    ],
)
def test_state_collapses_to_the_loop_vocabulary(name, type_, expected):
    assert _status_from_state(name, type_) == expected


def test_completed_tickets_are_filtered_out_of_list_assigned():
    c = client(
        assigned_response(
            [
                issue_node("TEAM-7"),
                issue_node("TEAM-8", state=("Done", "completed")),
            ]
        )
    )
    assert [t.identifier for t in c.list_assigned()] == ["TEAM-7"]


def test_in_review_tickets_are_returned_but_the_orchestrator_skips_them():
    from forman.orchestrator import select_ticket

    c = client(
        assigned_response([issue_node("TEAM-7", state=("In Review", "started"))])
    )
    tickets = c.list_assigned()
    assert tickets[0].status == "in_review"
    assert select_ticket(tickets) is None  # already at the gate, waiting on a human


# -- identifiers -------------------------------------------------------------


def test_split_identifier():
    assert split_identifier("TEAM-7") == ("TEAM", 7)
    assert split_identifier("ABC-142") == ("ABC", 142)


def test_split_identifier_rejects_nonsense():
    with pytest.raises(LinearApiError):
        split_identifier("not-a-ticket")


# -- errors ------------------------------------------------------------------


def test_graphql_errors_are_raised_not_swallowed():
    c = client({"errors": [{"message": "Authentication required"}]})
    with pytest.raises(LinearApiError, match="Authentication required"):
        c.list_assigned()


def test_missing_data_is_an_error():
    c = client({})
    with pytest.raises(LinearApiError):
        c.list_assigned()


# -- state lookup ------------------------------------------------------------


def team_response(states):
    return {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "id": "uuid-team",
                        "key": "TEAM",
                        "name": "Engineering",
                        "states": {"nodes": states},
                        "labels": {"nodes": []},
                    }
                ]
            }
        }
    }


def test_find_state_matches_the_exact_name_first():
    states = [
        {"id": "s1", "name": "In Review", "type": "started"},
        {"id": "s2", "name": "Reviewed", "type": "started"},
    ]
    c = client(team_response(states))
    assert c.find_state("TEAM", "in_review")["id"] == "s1"


def test_find_state_falls_back_to_a_substring():
    states = [{"id": "s9", "name": "Awaiting Code Review", "type": "started"}]
    c = client(team_response(states))
    assert c.find_state("TEAM", "in_review")["id"] == "s9"


def test_find_state_honours_an_explicit_override():
    states = [
        {"id": "s1", "name": "In Review", "type": "started"},
        {"id": "s2", "name": "Ready to ship", "type": "started"},
    ]
    c = client(team_response(states), review_state="Ready to ship")
    assert c.find_state("TEAM", "in_review")["id"] == "s2"


def test_find_state_fails_loudly_with_the_real_state_names():
    states = [{"id": "s1", "name": "Shipped", "type": "completed"}]
    c = client(team_response(states))
    with pytest.raises(LinearApiError, match="Shipped"):
        c.find_state("TEAM", "in_review")


# -- mutations ---------------------------------------------------------------


def test_comment_resolves_the_issue_uuid_then_posts():
    transport = FakeTransport(
        [
            {"data": {"issues": {"nodes": [issue_node("TEAM-7")]}}},
            {"data": {"commentCreate": {"success": True}}},
        ]
    )
    c = GraphQLLinearClient(api_key="k", transport=transport)
    c.comment("TEAM-7", "PR opened: http://x")

    assert transport.calls[1][1]["issueId"] == "uuid-TEAM-7"
    assert transport.calls[1][1]["body"] == "PR opened: http://x"


def test_set_status_looks_up_the_state_id():
    transport = FakeTransport(
        [
            team_response([{"id": "s1", "name": "In Review", "type": "started"}]),
            {"data": {"issues": {"nodes": [issue_node("TEAM-7")]}}},
            {"data": {"issueUpdate": {"success": True}}},
        ]
    )
    c = GraphQLLinearClient(api_key="k", transport=transport)
    c.set_status("TEAM-7", "in_review")

    assert transport.calls[-1][1] == {"issueId": "uuid-TEAM-7", "stateId": "s1"}


def test_failed_mutation_raises():
    transport = FakeTransport(
        [
            {"data": {"issues": {"nodes": [issue_node("TEAM-7")]}}},
            {"data": {"commentCreate": {"success": False}}},
        ]
    )
    c = GraphQLLinearClient(api_key="k", transport=transport)
    with pytest.raises(LinearApiError):
        c.comment("TEAM-7", "hi")


VIEWER = {
    "data": {
        "viewer": {
            "id": "uuid-viewer",
            "name": "Test User",
            "displayName": "testuser",
            "email": "test@example.com",
        }
    }
}

USERS = {
    "data": {
        "users": {
            "nodes": [
                {
                    "id": "uuid-viewer",
                    "name": "Test User",
                    "displayName": "testuser",
                    "email": "test@example.com",
                    "active": True,
                },
                {
                    "id": "uuid-mate",
                    "name": "Team Mate",
                    "displayName": "mate",
                    "email": "mate@example.com",
                    "active": True,
                },
            ]
        }
    }
}


def create_response(identifier="TEAM-42"):
    return {
        "data": {
            "issueCreate": {
                "success": True,
                "issue": {
                    "id": "uuid-new",
                    "identifier": identifier,
                    "url": f"http://x/{identifier}",
                },
            }
        }
    }


def test_create_maps_priority_and_returns_the_identifier():
    transport = FakeTransport([team_response([]), VIEWER, create_response()])
    c = GraphQLLinearClient(api_key="k", team_key="TEAM", transport=transport)
    made = c.create(Ticket(identifier="", title="New thing", priority="urgent"))

    assert made.identifier == "TEAM-42"
    assert made.url == "http://x/TEAM-42"
    assert transport.calls[-1][1]["input"]["priority"] == 1
    assert transport.calls[-1][1]["input"]["teamId"] == "uuid-team"


# -- identity ----------------------------------------------------------------


def test_created_tickets_are_assigned_or_the_pull_phase_can_never_see_them():
    transport = FakeTransport([team_response([]), VIEWER, create_response()])
    c = GraphQLLinearClient(api_key="k", team_key="TEAM", transport=transport)
    c.create(Ticket(identifier="", title="New thing"))

    assert transport.calls[-1][1]["input"]["assigneeId"] == "uuid-viewer"


def test_without_linear_user_the_api_key_is_the_identity():
    transport = FakeTransport(assigned_response([issue_node("TEAM-7")]))
    c = GraphQLLinearClient(api_key="k", transport=transport)
    c.list_assigned()

    # The viewer path: no user lookup, no name to get wrong.
    assert "assignedIssues" in transport.calls[0][0]
    assert transport.calls[0][1] == {"first": PAGE_SIZE}


def test_linear_user_switches_to_an_explicit_assignee_filter():
    transport = FakeTransport(
        [
            USERS,
            {"data": {"issues": {"nodes": [issue_node("TEAM-7")]}}},
        ]
    )
    c = GraphQLLinearClient(api_key="k", user="mate@example.com", transport=transport)
    tickets = c.list_assigned()

    assert [t.identifier for t in tickets] == ["TEAM-7"]
    assert transport.calls[-1][1] == {"first": PAGE_SIZE, "userId": "uuid-mate"}


def test_linear_user_also_decides_who_new_tickets_go_to():
    transport = FakeTransport([team_response([]), USERS, create_response()])
    c = GraphQLLinearClient(
        api_key="k", team_key="TEAM", user="mate@example.com", transport=transport
    )
    c.create(Ticket(identifier="", title="For a teammate"))

    assert transport.calls[-1][1]["input"]["assigneeId"] == "uuid-mate"


@pytest.mark.parametrize(
    "who", ["mate@example.com", "MATE@example.com", "mate", "Team Mate", "uuid-mate"]
)
def test_users_resolve_by_email_handle_name_or_id(who):
    c = GraphQLLinearClient(api_key="k", user=who, transport=FakeTransport(USERS))
    assert c.resolve_user(who)["id"] == "uuid-mate"


def test_unknown_user_fails_loudly_and_lists_real_members():
    c = GraphQLLinearClient(api_key="k", transport=FakeTransport(USERS))
    with pytest.raises(LinearApiError) as exc:
        c.resolve_user("nobody@example.com")
    assert "testuser" in str(exc.value) and "mate" in str(exc.value)


def test_create_drops_a_tshirt_estimate_rather_than_sending_junk():
    transport = FakeTransport([team_response([]), VIEWER, create_response("TEAM-43")])
    c = GraphQLLinearClient(api_key="k", team_key="TEAM", transport=transport)
    c.create(Ticket(identifier="", title="t", estimate="m"))
    assert "estimate" not in transport.calls[-1][1]["input"]


def test_create_without_a_team_key_and_several_teams_is_a_clear_error():
    transport = FakeTransport(
        {
            "data": {
                "teams": {
                    "nodes": [
                        {"id": "a", "key": "TEAM", "name": "Eng"},
                        {"id": "b", "key": "OTHER", "name": "Other Team"},
                    ]
                }
            }
        }
    )
    c = GraphQLLinearClient(api_key="k", transport=transport)
    with pytest.raises(LinearApiError, match="LINEAR_TEAM_KEY"):
        c.create(Ticket(identifier="", title="t"))


# -- config ------------------------------------------------------------------


def test_parse_env_handles_the_shapes_people_actually_write():
    parsed = parse_env(
        "# a comment\n"
        "\n"
        "LINEAR_API_KEY=lin_api_plain\n"
        'LINEAR_TEAM_KEY="TEAM"\n'
        "export LINEAR_REVIEW_STATE='In Review'"
    )
    assert parsed["LINEAR_API_KEY"] == "lin_api_plain"
    assert parsed["LINEAR_TEAM_KEY"] == "TEAM"
    assert parsed["LINEAR_REVIEW_STATE"] == "In Review"


def test_repo_env_file_is_read(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nowhere"))
    (tmp_path / ".env").write_text("LINEAR_API_KEY=lin_api_from_repo\n")

    assert load_settings(tmp_path).api_key == "lin_api_from_repo"


def test_shell_environment_beats_the_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nowhere"))
    (tmp_path / ".env").write_text("LINEAR_API_KEY=from_file\n")
    monkeypatch.setenv("LINEAR_API_KEY", "from_shell")

    assert load_settings(tmp_path).api_key == "from_shell"


def test_repo_env_beats_the_user_config(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    home = tmp_path / "config"
    (home / "forman").mkdir(parents=True)
    (home / "forman" / ".env").write_text("LINEAR_API_KEY=from_user_config\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))

    repo = tmp_path / "repo"
    repo.mkdir()
    assert load_settings(repo).api_key == "from_user_config"

    (repo / ".env").write_text("LINEAR_API_KEY=from_repo\n")
    assert load_settings(repo).api_key == "from_repo"


def test_missing_key_error_says_where_to_put_one(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nowhere"))

    with pytest.raises(Exception) as exc:
        load_settings(tmp_path).require_api_key(tmp_path)
    message = str(exc.value)
    assert "linear.app/settings/api" in message
    assert "--linear stub" in message
