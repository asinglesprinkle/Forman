"""The Linear boundary.

Linear only ever sees tickets. Sub-tasks never appear here.

Three implementations:

  LinearClient      - the Protocol every caller codes against.
  StubLinearClient  - JSON-file backed, no token, no network. The default, and
                      what the test suite runs against.
  McpLinearClient   - routes through the Linear MCP server by way of a
                      short-lived agent session. Off by default; see the note on
                      the class before turning it on.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .models import Ticket
from .spawn import AgentRun, extract_last_json, run_agent

STUB_FILE = "linear-stub.json"


@runtime_checkable
class LinearClient(Protocol):
    """Every Linear side effect the pipeline performs. Nothing else is allowed."""

    def list_assigned(self) -> list[Ticket]:
        """Tickets assigned to me that are not finished."""
        ...

    def get(self, identifier: str) -> Ticket:
        """One ticket by identifier, e.g. `TEAM-42`."""
        ...

    def comment(self, identifier: str, body: str) -> None:
        """Post a comment. Used for the PR link and for blocked/failed summaries."""
        ...

    def set_status(self, identifier: str, status: str) -> None:
        """Move the ticket. The pipeline only ever sets `in_review`."""
        ...

    def create(self, ticket: Ticket) -> Ticket:
        """Create a ticket from the push phase. Returns it with its identifier."""
        ...


# -- stub --------------------------------------------------------------------


class StubLinearClient:
    """A working Linear stand-in so the pipeline runs end to end with no token.

    Backed by a JSON file when given a path, so a run's comments and status
    changes are inspectable afterwards; purely in-memory otherwise.
    """

    def __init__(
        self,
        tickets: list[Ticket] | None = None,
        path: str | Path | None = None,
        next_number: int = 100,
        default_prefix: str = "TEAM",
    ) -> None:
        self.path = Path(path) if path else None
        self.tickets: dict[str, Ticket] = {t.identifier: t for t in (tickets or [])}
        self.comments: list[tuple[str, str]] = []
        self.status_changes: list[tuple[str, str]] = []
        self._next_number = next_number
        self._default_prefix = default_prefix
        if self.path and self.path.is_file() and not tickets:
            self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        self.tickets = {t["identifier"]: Ticket(**t) for t in raw.get("tickets", [])}
        self.comments = [tuple(c) for c in raw.get("comments", [])]  # type: ignore[misc]
        self.status_changes = [tuple(s) for s in raw.get("status_changes", [])]  # type: ignore[misc]
        self._next_number = raw.get("next_number", self._next_number)

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "tickets": [asdict(t) for t in self.tickets.values()],
                    "comments": [list(c) for c in self.comments],
                    "status_changes": [list(s) for s in self.status_changes],
                    "next_number": self._next_number,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # -- protocol ------------------------------------------------------------

    def list_assigned(self) -> list[Ticket]:
        return [t for t in self.tickets.values() if not t.is_done()]

    def get(self, identifier: str) -> Ticket:
        return self.tickets[identifier]

    def comment(self, identifier: str, body: str) -> None:
        self.comments.append((identifier, body))
        self._save()

    def set_status(self, identifier: str, status: str) -> None:
        self.tickets[identifier].status = status
        self.status_changes.append((identifier, status))
        self._save()

    def create(self, ticket: Ticket) -> Ticket:
        if not ticket.identifier:
            ticket.identifier = f"{self._default_prefix}-{self._next_number}"
            self._next_number += 1
        self.tickets[ticket.identifier] = ticket
        self._save()
        return ticket


# -- MCP ---------------------------------------------------------------------

LINEAR_MCP_URL = "https://mcp.linear.app/mcp"

LINEAR_MCP_TOOLS = [
    "mcp__linear__list_issues",
    "mcp__linear__get_issue",
    "mcp__linear__save_issue",
    "mcp__linear__save_comment",
    "mcp__linear__list_issue_statuses",
]

_MCP_CONTRACT = """\
You are a thin adapter between a pipeline and Linear. Use ONLY the Linear MCP \
tools to carry out exactly the one request below. Do not infer extra work, do \
not modify anything you were not asked to modify, and never touch an issue other \
than the one named. Emit as your final message a single JSON object and nothing \
else, matching the shape the request specifies. If the request cannot be \
completed, emit {"error": "what went wrong"}.\
"""


class McpLinearClient:
    """Talks to Linear through the Linear MCP server.

    How it works: each call runs a short-lived agent session with the Linear MCP
    server attached and only the Linear tools allowed, then parses a JSON object
    back out. That keeps the activity seam identical to the stub, and it works
    headless, but it does put a model in the path of reading ticket data, which
    is slower and less deterministic than a direct GraphQL call would be.

    Before using this you must register the Linear MCP server with the Claude
    Code CLI, for example:

        claude mcp add --transport http linear https://mcp.linear.app/mcp

    Status: written against the documented MCP tool names but NOT yet exercised
    against a live workspace. Treat the first run as a smoke test, on a ticket
    you do not mind touching. The stub remains the default.
    """

    def __init__(
        self,
        me: str = "me",
        url: str = LINEAR_MCP_URL,
        runner: Callable[..., AgentRun] = run_agent,
        cwd: str | Path = ".",
    ) -> None:
        self.me = me
        self.url = url
        self.runner = runner
        self.cwd = cwd

    # -- plumbing ------------------------------------------------------------

    def _call(self, request: str) -> dict[str, Any]:
        run = self.runner(
            prompt=request,
            system_prompt=_MCP_CONTRACT,
            cwd=self.cwd,
            allowed_tools=LINEAR_MCP_TOOLS,
            max_turns=12,
            mcp_servers={"linear": {"type": "http", "url": self.url}},
        )
        if run.error:
            raise LinearError(f"Linear MCP session failed: {run.error}")
        payload = extract_last_json(run.text)
        if payload is None:
            raise LinearError(f"Linear MCP returned no JSON. Raw output:\n{run.text}")
        if "error" in payload:
            raise LinearError(str(payload["error"]))
        return payload

    @staticmethod
    def _ticket_from(raw: dict[str, Any]) -> Ticket:
        return Ticket(
            identifier=str(raw.get("identifier", "")),
            title=str(raw.get("title", "")),
            description=str(raw.get("description") or ""),
            status=str(raw.get("status") or "todo").lower(),
            priority=str(raw.get("priority") or "medium"),
            labels=[str(x) for x in raw.get("labels") or []],
            project=raw.get("project"),
            estimate=raw.get("estimate"),
            blocked_by=[str(x) for x in raw.get("blocked_by") or []],
            blocks=[str(x) for x in raw.get("blocks") or []],
            url=raw.get("url"),
        )

    # -- protocol ------------------------------------------------------------

    def list_assigned(self) -> list[Ticket]:
        payload = self._call(
            f"List the unfinished issues assigned to {self.me}. For each, return "
            "identifier, title, description, status, priority, labels, project, "
            "estimate, url, and the identifiers of issues that block it "
            "(blocked_by) and that it blocks (blocks). Respond as "
            '{"issues": [ ... ]}.'
        )
        return [self._ticket_from(x) for x in payload.get("issues", [])]

    def get(self, identifier: str) -> Ticket:
        payload = self._call(
            f"Fetch issue {identifier}. Return identifier, title, description, "
            "status, priority, labels, project, estimate, url, blocked_by, and "
            'blocks as {"issue": { ... }}.'
        )
        return self._ticket_from(payload.get("issue", {}))

    def comment(self, identifier: str, body: str) -> None:
        self._call(
            f"Add exactly one comment to issue {identifier}. The comment body is "
            f"everything between the markers, verbatim:\n"
            f"<<<BODY\n{body}\nBODY\n"
            'Respond as {"ok": true}.'
        )

    def set_status(self, identifier: str, status: str) -> None:
        self._call(
            f"Move issue {identifier} to the workflow state that means "
            f"'{status}'. Look up the team's available statuses first and pick "
            "the closest match. Change nothing else. Respond as "
            '{"ok": true, "status": "<the state you set>"}.'
        )

    def create(self, ticket: Ticket) -> Ticket:
        payload = self._call(
            "Create one Linear issue with these fields:\n"
            f"{json.dumps(asdict(ticket), indent=2)}\n"
            'Respond as {"issue": {"identifier": "...", "url": "..."}}.'
        )
        created = payload.get("issue", {})
        ticket.identifier = str(created.get("identifier", ticket.identifier))
        ticket.url = created.get("url", ticket.url)
        return ticket


class LinearError(RuntimeError):
    """Any failure talking to Linear."""


def stub_path(repo_root: str | Path) -> Path:
    from .state import FOREMAN_DIR

    return Path(repo_root) / FOREMAN_DIR / STUB_FILE
