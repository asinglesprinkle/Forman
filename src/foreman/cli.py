"""`foreman` command line entry point.

Run it from inside the target repo. cwd is the codebase; bookkeeping lands in
`.foreman/<TICKET>/` there and is kept out of commits via .git/info/exclude.
Working more than one board just means running it from more than one directory.

    cd ~/code/service-a && foreman pull   # branch <team-key>-<number>/<slug>
    cd ~/code/service-b && foreman pull   # a different board, same command
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import git_ops
from .config import MissingApiKey, load_settings
from .decompose import decompose as decompose_ticket
from .linear_client import LinearClient, McpLinearClient, StubLinearClient, stub_path
from .linear_graphql import GraphQLLinearClient, LinearApiError
from .models import CommitResult, PullRequest, SpawnResult, SubTask, Ticket
from .orchestrator import Deps, run_once
from .spawn import spawn_agent
from .state import StateStore


class GitAdapter:
    """git_ops bound to one repo, satisfying the orchestrator's GitPort."""

    def __init__(self, repo: str | Path) -> None:
        self.repo = Path(repo)

    def require_clean(self) -> None:
        git_ops.require_clean(self.repo)

    def default_branch(self) -> str:
        return git_ops.default_branch(self.repo)

    def sync_default_branch(self) -> str:
        return git_ops.sync_default_branch(self.repo)

    def branch_name(self, identifier: str, title: str) -> str:
        return git_ops.branch_name(identifier, title)

    def create_branch(self, name: str) -> str:
        return git_ops.create_branch(self.repo, name)

    def commit(self, message: str) -> CommitResult:
        return git_ops.commit_all(self.repo, message)

    def push(self, branch: str) -> bool:
        return git_ops.push_branch(self.repo, branch)

    def open_pull_request(
        self, branch: str, base: str, title: str, body: str
    ) -> PullRequest:
        return git_ops.open_pull_request(self.repo, branch, base, title, body)


def resolve_repo(path: str | Path | None) -> Path:
    start = Path(path or Path.cwd()).resolve()
    if not git_ops.is_repo(start):
        raise SystemExit(
            f"{start} is not a git repository. Run foreman from inside the "
            "codebase you want it to work on."
        )
    return git_ops.repo_root(start)


def build_linear(repo: Path, backend: str) -> LinearClient:
    """Pick a Linear backend.

    `graphql` is the default and talks to the real API with a personal key.
    `stub` is offline. `mcp` routes through the Linear MCP server and is kept
    for the case where you would rather not hold a key at all.
    """
    if backend == "stub":
        return StubLinearClient(path=stub_path(repo))
    if backend == "mcp":
        return McpLinearClient(cwd=repo)

    settings = load_settings(repo)
    return GraphQLLinearClient(
        api_key=settings.require_api_key(repo),
        team_key=settings.team_key,
        review_state=settings.review_state,
        user=settings.user,
    )


def build_deps(repo: Path, linear: LinearClient) -> Deps:
    store = StateStore(repo)

    def decompose(ticket: Ticket) -> list[SubTask]:
        return decompose_ticket(ticket=ticket, store=store, cwd=repo)

    def spawn(
        *,
        ticket: Ticket,
        subtask: SubTask,
        readme: str,
        siblings: list[tuple[str, str]],
        attempt: int = 1,
        previous_error: str | None = None,
    ) -> SpawnResult:
        return spawn_agent(
            subtask_readme=readme,
            parent_ticket=ticket,
            repo_paths=[str(repo)],
            sibling_logs=siblings,
            cwd=repo,
            attempt=attempt,
            previous_error=previous_error,
        )

    return Deps(
        linear=linear,
        store=store,
        git=GitAdapter(repo),
        decompose=decompose,
        spawn=spawn,
    )


# -- commands ----------------------------------------------------------------


def cmd_pull(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    git_ops.ensure_ignored(repo)
    git_ops.ensure_ignored(repo, ".env")

    try:
        linear = build_linear(repo, args.linear)
        report = run_once(build_deps(repo, linear), ticket_id=args.ticket)
    except MissingApiKey as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LinearApiError as exc:
        print(f"Linear: {exc}", file=sys.stderr)
        return 2
    except git_ops.DirtyWorktree as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        print("commit or stash your changes, then run foreman again.", file=sys.stderr)
        return 2
    except git_ops.GitError as exc:
        print(f"git failed: {exc}", file=sys.stderr)
        if "could not read Username" in exc.stderr or "Authentication failed" in exc.stderr:
            print(
                "\ngit cannot authenticate to the remote. If you use HTTPS remotes "
                "and the gh CLI, run:\n\n    gh auth setup-git\n\nwhich points git's "
                "credential helper at your existing gh login.",
                file=sys.stderr,
            )
        return 2

    if report.outcome == "no_work":
        print(report.detail or "nothing ready to work on.")
        return 0

    print(f"{report.ticket} on branch {report.branch}")
    for row in report.subtasks:
        marker = {"done": "x", "blocked": "!", "failed": "X"}.get(row["status"], " ")
        suffix = f"  ({row['reason']})" if row["reason"] else ""
        print(f"  [{marker}] {row['id']} {row['goal']}{suffix}")

    for note in report.notes:
        print(f"\nnote: {note}")

    if report.outcome == "in_review":
        print(f"\n{report.detail}")
        print(f"pull request: {report.pr_url or '(open it by hand, see the ticket comment)'}")
        return 0

    print(f"\nhalted: {report.detail}")
    return 1


def cmd_push(args: argparse.Namespace) -> int:
    from .push import push, summarize

    repo = resolve_repo(args.repo)
    prose = args.prose or sys.stdin.read()
    if not prose.strip():
        print("nothing to push: give prose as an argument or on stdin.", file=sys.stderr)
        return 2

    try:
        tickets = push(
            prose=prose,
            linear=build_linear(repo, args.linear),
            cwd=repo,
            dry_run=args.dry_run,
        )
    except MissingApiKey as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LinearApiError as exc:
        print(f"Linear: {exc}", file=sys.stderr)
        return 2

    print(summarize(tickets))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Prove the API key works and show what Foreman can actually see.

    Read-only. Run this once after setting up the key, before letting the
    pipeline touch a real ticket.
    """
    repo = resolve_repo(args.repo)
    settings = load_settings(repo)

    try:
        client = GraphQLLinearClient(
            api_key=settings.require_api_key(repo),
            team_key=settings.team_key,
            review_state=settings.review_state,
            user=settings.user,
        )
        info = client.check()
    except MissingApiKey as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LinearApiError as exc:
        print(f"Linear: {exc}", file=sys.stderr)
        return 1

    print(f"authenticated as: {info['viewer']}")
    if not info["actor_is_viewer"]:
        print(f"acting as:        {info['actor']}  (from LINEAR_USER)")
    print(f"assigning to:     {info['actor']}")
    print(f"teams visible:    {', '.join(info['teams']) or '(none)'}")
    print(f"team in use:      {info['team_key'] or '(set LINEAR_TEAM_KEY)'}")
    print(f"workflow states:  {', '.join(info['states']) or '(unknown)'}")

    try:
        review = client.find_state(info["team_key"], "in_review") if info["team_key"] else None
        print(f"in-review state:  {review['name']}" if review else "in-review state:  (unknown)")
    except LinearApiError as exc:
        print(f"in-review state:  NOT FOUND. {exc}")

    print(f"\nassigned to you ({len(info['assigned'])} shown):")
    for ticket in info["assigned"]:
        blockers = f"  blocked_by={ticket.blocked_by}" if ticket.blocked_by else ""
        print(f"  {ticket.identifier}  [{ticket.status}/{ticket.priority}]  {ticket.title}{blockers}")
    if not info["assigned"]:
        print("  (nothing assigned, or nothing open)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    store = StateStore(repo)
    tickets = store.tickets()
    if not tickets:
        print("no foreman state in this repo yet.")
        return 0
    for ticket in tickets:
        state = store.load(ticket)
        done = len(state.done_ids())
        print(f"{state.ticket}  {state.status}  {done}/{len(state.subtasks)}  {state.branch}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foreman", description=__doc__)
    parser.add_argument(
        "--repo", default=None, help="target repo (defaults to the current directory)"
    )
    parser.add_argument(
        "--linear",
        choices=["graphql", "stub", "mcp"],
        default="graphql",
        help="Linear backend. `graphql` (default) uses LINEAR_API_KEY against "
        "the real API. `stub` is offline. `mcp` routes through the Linear MCP "
        "server instead of a key.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pull = sub.add_parser("pull", help="pull one ticket and take it to the gate")
    p_pull.add_argument(
        "--ticket",
        default=None,
        metavar="ID",
        help="work this exact ticket (e.g. TEAM-42) instead of letting Foreman "
        "choose. Skips the readiness checks.",
    )
    p_pull.set_defaults(func=cmd_pull)

    p_push = sub.add_parser("push", help="turn prose into Linear tickets")
    p_push.add_argument("prose", nargs="?", help="issue description (or pipe it on stdin)")
    p_push.add_argument(
        "--dry-run", action="store_true", help="show the tickets without creating them"
    )
    p_push.set_defaults(func=cmd_push)

    p_status = sub.add_parser("status", help="show foreman state in this repo")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser(
        "doctor", help="check the Linear API key and show what Foreman can see"
    )
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
