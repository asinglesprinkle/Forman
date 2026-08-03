"""Linear over its GraphQL API.

The default backend. One HTTP round trip per operation, no model in the path of
reading ticket data, and testable without an agent session.

Auth is a personal API key in the Authorization header with no `Bearer` prefix;
that prefix is for OAuth tokens and Linear rejects it on a personal key.

Transport is stdlib urllib so the tool stays dependency-light. It is injectable,
which is how the tests exercise every query shape below without a network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .models import Ticket

API_URL = "https://api.linear.app/graphql"
DEFAULT_TIMEOUT = 30.0

# Linear scores every query for complexity and rejects anything over 10,000.
# The issue fragment below is nested (labels, project, team, and both relation
# edges), so it costs roughly 230 per issue: 100 issues measured at 22,731 and
# was refused outright. 25 leaves comfortable headroom. It is how much we ask
# for per round trip, not how much we are willing to see.
PAGE_SIZE = 25

# Lookup queries select an id and a name and nothing nested, so they cost
# almost nothing and can take Linear's largest allowed page.
SHALLOW_PAGE_SIZE = 250

# Linear stores priority as an int. 0 means "no priority", which we treat as the
# lowest rung so unprioritized work never outranks something explicitly set.
_INT_TO_PRIORITY = {0: "low", 1: "urgent", 2: "high", 3: "medium", 4: "low"}
_PRIORITY_TO_INT = {"urgent": 1, "high": 2, "medium": 3, "low": 4}

_ISSUE_FIELDS = """
  id
  identifier
  title
  description
  url
  priority
  estimate
  state { name type }
  labels { nodes { name } }
  project { name }
  team { id key }
  relations { nodes { type relatedIssue { identifier } } }
  inverseRelations { nodes { type issue { identifier } } }
"""

_ASSIGNED_QUERY = f"""
query FormanAssigned($first: Int!, $after: String) {{
  viewer {{
    id
    name
    assignedIssues(
      first: $first
      after: $after
      filter: {{ completedAt: {{ null: true }}, canceledAt: {{ null: true }} }}
    ) {{
      nodes {{ {_ISSUE_FIELDS} }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

_BY_IDENTIFIER_QUERY = f"""
query FormanIssue($number: Float!, $teamKey: String!) {{
  issues(
    first: 1
    filter: {{ number: {{ eq: $number }}, team: {{ key: {{ eq: $teamKey }} }} }}
  ) {{
    nodes {{ {_ISSUE_FIELDS} }}
  }}
}}
"""

_TEAM_STATES_QUERY = """
query FormanStates($teamKey: String!) {
  teams(first: 1, filter: { key: { eq: $teamKey } }) {
    nodes {
      id
      key
      name
      states { nodes { id name type position } }
      labels { nodes { id name } }
    }
  }
}
"""

_ASSIGNED_TO_QUERY = f"""
query FormanAssignedTo($first: Int!, $after: String, $userId: ID!) {{
  issues(
    first: $first
    after: $after
    filter: {{
      assignee: {{ id: {{ eq: $userId }} }}
      completedAt: {{ null: true }}
      canceledAt: {{ null: true }}
    }}
  ) {{
    nodes {{ {_ISSUE_FIELDS} }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""

_VIEWER_QUERY = """
query FormanViewer { viewer { id name displayName email } }
"""

_USERS_QUERY = """
query FormanUsers($first: Int!, $after: String) {
  users(first: $first, after: $after) {
    nodes { id name displayName email active }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_TEAMS_QUERY = """
query FormanTeams($first: Int!, $after: String) {
  teams(first: $first, after: $after) {
    nodes { id key name }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_COMMENT_MUTATION = """
mutation FormanComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) { success }
}
"""

_UPDATE_STATE_MUTATION = """
mutation FormanSetState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) { success }
}
"""

_CREATE_MUTATION = """
mutation FormanCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier url }
  }
}
"""

_RELATION_MUTATION = """
mutation FormanRelate($issueId: String!, $relatedIssueId: String!) {
  issueRelationCreate(
    input: { issueId: $issueId, relatedIssueId: $relatedIssueId, type: blocks }
  ) { success }
}
"""

_PROJECTS_QUERY = """
query FormanProjects($first: Int!, $after: String) {
  projects(first: $first, after: $after) {
    nodes { id name }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class LinearApiError(RuntimeError):
    """Any failure talking to the Linear API."""


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


def paginate(
    query_fn: Callable[..., dict[str, Any]],
    query: str,
    *path: str,
    first: int = SHALLOW_PAGE_SIZE,
    limit: int | None = None,
    **variables: Any,
) -> list[dict[str, Any]]:
    """Walk a Linear connection to the end and return every node.

    Asking for `first: N` and reading the nodes gets you N of them and no hint
    that there was ever an N+1. That silence is the whole problem: a project on
    the second page reads back as a project that does not exist, and the caller
    goes on to file the work somewhere else.

    `path` names the connection inside the response body — ("users",) for a
    top-level one, ("viewer", "assignedIssues") for the nested one. The query
    has to declare `$first: Int!` and `$after: String` and select
    `pageInfo { hasNextPage endCursor }`. Without that pageInfo this stops
    after one page, which is the old truncating behaviour rather than a spin.

    `first` is the page size, not the answer size: Linear allows at most 250,
    and its complexity scorer allows far less for a nested fragment. `limit`
    caps the answer and stops the walk early.

    Takes the query callable rather than a client so Red can page its own
    project queries through the same walk.
    """
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    seen: set[str] = set()

    while True:
        page = min(first, limit - len(nodes)) if limit else first
        body: Any = query_fn(query, first=page, after=after, **variables)
        for key in path:
            body = body[key]

        nodes.extend(body.get("nodes") or [])
        if limit is not None and len(nodes) >= limit:
            return nodes[:limit]

        info = body.get("pageInfo") or {}
        after = info.get("endCursor")
        # A server claiming another page without moving the cursor would spin
        # here forever, so a missing or repeated cursor counts as the end.
        if not info.get("hasNextPage") or not after or after in seen:
            return nodes
        seen.add(after)


def _status_from_state(name: str, type_: str) -> str:
    """Collapse a Linear workflow state into the vocabulary the loop reasons in.

    The loop needs three answers: is this finished (skip it), is it sitting at
    the human gate (skip it), or is it workable. Team state names vary wildly,
    so match on `type` where possible and fall back to the name.
    """
    type_ = (type_ or "").lower()
    lowered = (name or "").lower()
    if type_ in {"completed", "canceled", "cancelled"}:
        return "done"
    if "review" in lowered:
        return "in_review"
    if type_ == "started":
        return "in_progress"
    return "todo"


def _ticket_from_node(node: dict[str, Any]) -> Ticket:
    state = node.get("state") or {}
    labels = [n["name"] for n in (node.get("labels") or {}).get("nodes", [])]
    project = (node.get("project") or {}).get("name")

    # An IssueRelation of type "blocks" points from blocker to blocked. So this
    # issue's own relations name what it blocks, and its inverse relations name
    # what blocks it.
    blocks = [
        (r.get("relatedIssue") or {}).get("identifier")
        for r in (node.get("relations") or {}).get("nodes", [])
        if r.get("type") == "blocks"
    ]
    blocked_by = [
        (r.get("issue") or {}).get("identifier")
        for r in (node.get("inverseRelations") or {}).get("nodes", [])
        if r.get("type") == "blocks"
    ]

    estimate = node.get("estimate")
    return Ticket(
        identifier=node.get("identifier", ""),
        title=node.get("title", ""),
        description=node.get("description") or "",
        status=_status_from_state(state.get("name", ""), state.get("type", "")),
        priority=_INT_TO_PRIORITY.get(node.get("priority") or 0, "low"),
        labels=labels,
        project=project,
        estimate=str(estimate) if estimate is not None else None,
        blocked_by=[b for b in blocked_by if b],
        blocks=[b for b in blocks if b],
        url=node.get("url"),
    )


def split_identifier(identifier: str) -> tuple[str, int]:
    """`TEAM-42` -> ("TEAM", 42). Raises on anything that is not an issue id."""
    prefix, _, number = identifier.rpartition("-")
    if not prefix or not number.isdigit():
        raise LinearApiError(f"not a Linear issue identifier: {identifier!r}")
    return prefix, int(number)


class GraphQLLinearClient:
    """Satisfies the LinearClient protocol against the real Linear API."""

    def __init__(
        self,
        api_key: str,
        team_key: str | None = None,
        review_state: str | None = None,
        user: str | None = None,
        url: str = API_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ) -> None:
        self.api_key = api_key
        self.team_key = team_key
        self.review_state = review_state
        # Optional. Left unset, the API key IS the identity: Linear resolves
        # `viewer` server-side, which cannot drift the way a name can. Set it
        # only to act as someone else, e.g. a shared key filing work for a
        # teammate.
        self.user = user
        self.url = url
        self.timeout = timeout
        self._transport = transport or self._http
        self._issue_ids: dict[str, str] = {}
        self._teams: dict[str, dict] | None = None
        self._viewer: dict | None = None
        self._users: list[dict] | None = None

    # -- transport -----------------------------------------------------------

    def _http(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                # No `Bearer` prefix: that is for OAuth tokens, not personal keys.
                "Authorization": self.api_key,
                "User-Agent": "forman/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (401, 403):
                raise LinearApiError(
                    f"Linear rejected the API key ({exc.code}). Check LINEAR_API_KEY. {detail}"
                ) from exc
            raise LinearApiError(f"Linear returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LinearApiError(f"could not reach Linear: {exc.reason}") from exc
        return body

    def query(self, query: str, **variables: Any) -> dict[str, Any]:
        body = self._transport(query, variables)
        if body.get("errors"):
            messages = "; ".join(e.get("message", str(e)) for e in body["errors"])
            raise LinearApiError(f"Linear GraphQL error: {messages}")
        data = body.get("data")
        if data is None:
            raise LinearApiError(f"Linear returned no data: {body}")
        return data

    # -- lookups -------------------------------------------------------------

    def _remember(self, node: dict[str, Any]) -> None:
        if node.get("identifier") and node.get("id"):
            self._issue_ids[node["identifier"]] = node["id"]

    def issue_id(self, identifier: str) -> str:
        """Resolve `TEAM-42` to the UUID the mutations need."""
        if identifier not in self._issue_ids:
            self.get(identifier)  # populates the cache
        if identifier not in self._issue_ids:
            raise LinearApiError(f"issue not found: {identifier}")
        return self._issue_ids[identifier]

    def viewer(self) -> dict[str, Any]:
        """Whoever the API key belongs to."""
        cached = self._viewer
        if cached is None:
            cached = self._viewer = self.query(_VIEWER_QUERY)["viewer"]
        return cached

    def users(self) -> list[dict[str, Any]]:
        cached = self._users
        if cached is None:
            cached = self._users = paginate(self.query, _USERS_QUERY, "users")
        return cached

    def resolve_user(self, who: str) -> dict[str, Any]:
        """Find a workspace member by email, display name, full name, or id.

        Matching is case-insensitive and checks the most specific field first,
        because display names collide far more often than emails do.
        """
        wanted = who.strip().lower()
        members = self.users()

        for field in ("email", "displayName", "name", "id"):
            for member in members:
                value = member.get(field)
                if value and str(value).strip().lower() == wanted:
                    return member

        active = [
            m.get("displayName") or m.get("name") or m.get("email")
            for m in members
            if m.get("active", True)
        ]
        shown = ", ".join(sorted(filter(None, active))[:20])
        raise LinearApiError(
            f"no Linear user matching {who!r}. Known members include: {shown}. "
            "Set LINEAR_USER to an email, display name, or user id, or unset it "
            "to just use whoever owns the API key."
        )

    def actor(self) -> dict[str, Any]:
        """The person we filter tickets for and assign new tickets to."""
        return self.resolve_user(self.user) if self.user else self.viewer()

    def team(self, key: str) -> dict[str, Any]:
        """Team record including its workflow states and labels."""
        if self._teams is None:
            self._teams = {}
        if key not in self._teams:
            nodes = self.query(_TEAM_STATES_QUERY, teamKey=key)["teams"]["nodes"]
            if not nodes:
                raise LinearApiError(f"no Linear team with key {key!r}")
            self._teams[key] = nodes[0]
        return self._teams[key]

    def teams(self) -> list[dict[str, Any]]:
        return paginate(self.query, _TEAMS_QUERY, "teams")

    def find_state(self, team_key: str, wanted: str) -> dict[str, Any]:
        """Find the workflow state to move an issue into.

        Team state names are arbitrary, so this matches generously: an exact
        name, then a substring, then anything of the same Linear state type.
        Failing that it raises with the real state list, because guessing wrong
        here moves someone's ticket to the wrong column.
        """
        states = self.team(team_key)["states"]["nodes"]
        target = (self.review_state or wanted).replace("_", " ").strip().lower()

        for state in states:
            if state["name"].strip().lower() == target:
                return state
        for state in states:
            if target in state["name"].strip().lower():
                return state
        if "review" in target:
            for state in states:
                if "review" in state["name"].strip().lower():
                    return state
        available = ", ".join(s["name"] for s in states)
        raise LinearApiError(
            f"no workflow state on team {team_key} matching {target!r}. "
            f"Available: {available}. Set LINEAR_REVIEW_STATE to the exact name."
        )

    def default_team_key(self) -> str:
        if self.team_key:
            return self.team_key
        found = self.teams()
        if len(found) == 1:
            return found[0]["key"]
        keys = ", ".join(t["key"] for t in found) or "(none)"
        raise LinearApiError(
            f"more than one Linear team is visible ({keys}); set LINEAR_TEAM_KEY "
            "to the one you want to create issues on."
        )

    # -- protocol ------------------------------------------------------------

    def list_assigned(self, first: int | None = None) -> list[Ticket]:
        """Every open ticket for the acting user.

        `first` caps how many come back; the default walks to the end. The walk
        terminates on its own because the query is narrow: one assignee, with
        completed and canceled issues filtered out server-side. It is one
        person's open backlog, not the workspace.

        PAGE_SIZE per round trip is Linear's complexity limit talking, not a
        ceiling on the answer.
        """
        if self.user:
            nodes = paginate(
                self.query,
                _ASSIGNED_TO_QUERY,
                "issues",
                first=PAGE_SIZE,
                limit=first,
                userId=self.actor()["id"],
            )
        else:
            nodes = paginate(
                self.query,
                _ASSIGNED_QUERY,
                "viewer",
                "assignedIssues",
                first=PAGE_SIZE,
                limit=first,
            )

        tickets = []
        for node in nodes:
            self._remember(node)
            ticket = _ticket_from_node(node)
            if ticket.is_done():  # belt and braces over the server-side filter
                continue
            tickets.append(ticket)
        return tickets

    def get(self, identifier: str) -> Ticket:
        team_key, number = split_identifier(identifier)
        nodes = self.query(
            _BY_IDENTIFIER_QUERY, number=float(number), teamKey=team_key
        )["issues"]["nodes"]
        if not nodes:
            raise LinearApiError(f"issue not found: {identifier}")
        self._remember(nodes[0])
        return _ticket_from_node(nodes[0])

    def comment(self, identifier: str, body: str) -> None:
        result = self.query(
            _COMMENT_MUTATION, issueId=self.issue_id(identifier), body=body
        )
        if not result["commentCreate"]["success"]:
            raise LinearApiError(f"Linear refused the comment on {identifier}")

    def set_status(self, identifier: str, status: str) -> None:
        team_key, _ = split_identifier(identifier)
        state = self.find_state(team_key, status)
        result = self.query(
            _UPDATE_STATE_MUTATION,
            issueId=self.issue_id(identifier),
            stateId=state["id"],
        )
        if not result["issueUpdate"]["success"]:
            raise LinearApiError(f"Linear refused the state change on {identifier}")

    def create(self, ticket: Ticket) -> Ticket:
        team_key = self.team_key or self.default_team_key()
        team = self.team(team_key)

        payload: dict[str, Any] = {
            "teamId": team["id"],
            "title": ticket.title,
            "description": ticket.description,
            "priority": _PRIORITY_TO_INT.get(ticket.priority, 3),
            # Without this the push phase and the pull phase never meet: the
            # pull phase looks for tickets assigned to someone, so an
            # unassigned ticket is invisible to it forever.
            "assigneeId": self.actor()["id"],
        }
        if ticket.estimate and str(ticket.estimate).isdigit():
            # Linear estimates are numeric points and only accepted when the
            # team has estimates enabled. A t-shirt size is dropped on purpose.
            payload["estimate"] = int(ticket.estimate)
        if ticket.labels:
            by_name = {
                label["name"].lower(): label["id"]
                for label in team.get("labels", {}).get("nodes", [])
            }
            ids = [
                by_name[name.lower()]
                for name in ticket.labels
                if name.lower() in by_name
            ]
            if ids:
                payload["labelIds"] = ids
        if ticket.project:
            for project in paginate(self.query, _PROJECTS_QUERY, "projects"):
                if project["name"].lower() == ticket.project.lower():
                    payload["projectId"] = project["id"]
                    break

        result = self.query(_CREATE_MUTATION, input=payload)["issueCreate"]
        if not result["success"]:
            raise LinearApiError(f"Linear refused to create {ticket.title!r}")

        created = result["issue"]
        ticket.identifier = created["identifier"]
        ticket.url = created.get("url")
        self._issue_ids[ticket.identifier] = created["id"]

        # Ordering only counts if it exists in Linear, since the pull phase's
        # ticket-level sort reads exactly these relations back.
        for blocker in ticket.blocked_by:
            try:
                self.relate_blocks(blocker, ticket.identifier)
            except LinearApiError:
                pass  # a missing blocker should not undo a created ticket
        return ticket

    def relate_blocks(self, blocker: str, blocked: str) -> None:
        """Record that `blocker` blocks `blocked`."""
        result = self.query(
            _RELATION_MUTATION,
            issueId=self.issue_id(blocker),
            relatedIssueId=self.issue_id(blocked),
        )
        if not result["issueRelationCreate"]["success"]:
            raise LinearApiError(
                f"Linear refused the relation {blocker} blocks {blocked}"
            )

    # -- diagnostics ---------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """One read-only round trip used by `forman doctor`.

        Every schema assumption in this file shows up in the output, so a
        mismatch is visible immediately rather than mid-run.
        """
        viewer = self.viewer()
        actor = self.actor()
        issues = self.list_assigned(first=5)
        teams = self.teams()
        team_key = self.team_key or (teams[0]["key"] if len(teams) == 1 else None)
        states = []
        if team_key:
            states = [s["name"] for s in self.team(team_key)["states"]["nodes"]]
        return {
            "viewer": viewer.get("email") or viewer.get("name"),
            "actor": actor.get("displayName")
            or actor.get("name")
            or actor.get("email"),
            "actor_is_viewer": actor.get("id") == viewer.get("id"),
            "teams": [t["key"] for t in teams],
            "team_key": team_key,
            "states": states,
            "assigned": issues,
        }
