"""The Linear boundary.

Linear only ever sees tickets. Sub-tasks never appear here.

Two implementations here:

  LinearClient      - the Protocol every caller codes against.
  StubLinearClient  - JSON-file backed, no token, no network. What the test
                      suite runs against, and `--linear stub` for offline use.

The real one lives in linear_graphql.py.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import Ticket

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


def stub_path(repo_root: str | Path) -> Path:
    from .state import FORMAN_DIR

    return Path(repo_root) / FORMAN_DIR / STUB_FILE
