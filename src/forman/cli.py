"""`forman` command line entry point.

Run it from inside the target repo. cwd is the codebase; bookkeeping lands in
`.forman/<TICKET>/` there and is kept out of commits via .git/info/exclude.
Working more than one board just means running it from more than one directory.

    cd ~/code/service-a && forman pull   # branch <team-key>-<number>/<slug>
    cd ~/code/service-b && forman pull   # a different board, same command
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import git_ops
from .config import (
    API_KEY_VAR,
    ENV_FILE,
    REVIEW_STATE_VAR,
    TEAM_KEY_VAR,
    USER_VAR,
    MissingApiKey,
    load_settings,
    mask,
    user_config_dir,
    write_user_env,
)
from .decompose import decompose as decompose_ticket
from .linear_client import LinearClient, StubLinearClient, stub_path
from .linear_graphql import GraphQLLinearClient, LinearApiError
from .models import CommitResult, PullRequest, SpawnResult, SubTask, Ticket
from .orchestrator import Deps, run_once
from .progress import ask_after_agent, for_terminal
from .spawn import spawn_agent
from .state import StateStore, format_cost

# Imported for its import side effect, not for anything it exports: this is what
# makes `input()` use GNU readline instead of the terminal driver's canonical
# mode. The driver erases with backspace-space-backspace, which cannot cross a
# row boundary, so editing a line that has wrapped stops dead at the start of
# the current visual row - the characters go, the display does not follow. The
# prose prompt in `forman push` invites exactly the input that wraps. Readline
# also brings arrow keys, ^A/^E/^W/^U, and recall within a run.
#
# Do not "clean up" this import. Not available on every build, hence the guard.
try:  # pragma: no cover - presence is a property of the interpreter build
    import readline  # noqa: F401
except ImportError:  # pragma: no cover
    pass


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
            f"{start} is not a git repository. Run forman from inside the "
            "codebase you want it to work on."
        )
    return git_ops.repo_root(start)


def build_linear(repo: Path, backend: str) -> LinearClient:
    """Pick a Linear backend.

    `graphql` is the default and talks to the real API with a personal key.
    `stub` is offline, backed by a JSON file, and needs no account.
    """
    if backend == "stub":
        return StubLinearClient(path=stub_path(repo))

    settings = load_settings(repo)
    return GraphQLLinearClient(
        api_key=settings.require_api_key(repo),
        team_key=settings.team_key,
        review_state=settings.review_state,
        user=settings.user,
    )


def build_deps(repo: Path, linear: LinearClient, progress=None) -> Deps:
    store = StateStore(repo)

    def decompose(ticket: Ticket) -> list[SubTask]:
        if progress:
            progress.start(f"{ticket.identifier}: splitting into sub-tasks")
        try:
            return decompose_ticket(
                ticket=ticket, store=store, cwd=repo, on_activity=progress
            )
        finally:
            if progress:
                progress.done()

    def spawn(
        *,
        ticket: Ticket,
        subtask: SubTask,
        readme: str,
        siblings: list[tuple[str, str]],
        attempt: int = 1,
        previous_error: str | None = None,
    ) -> SpawnResult:
        # The longest silence in the whole pipeline: this one edits, runs the
        # tests and commits, and until now said nothing until it was done.
        if progress:
            retry = f" (attempt {attempt})" if attempt > 1 else ""
            progress.start(f"{subtask.id}: {subtask.goal}{retry}")
        try:
            return spawn_agent(
                subtask_readme=readme,
                parent_ticket=ticket,
                repo_paths=[str(repo)],
                sibling_logs=siblings,
                cwd=repo,
                attempt=attempt,
                previous_error=previous_error,
                on_activity=progress,
            )
        finally:
            if progress:
                progress.done()

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
        report = run_once(
            build_deps(repo, linear, progress=for_terminal()), ticket_id=args.ticket
        )
    except MissingApiKey as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LinearApiError as exc:
        print(f"Linear: {exc}", file=sys.stderr)
        return 2
    except git_ops.DirtyWorktree as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        print("commit or stash your changes, then run forman again.", file=sys.stderr)
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
        print(f"total cost: {format_cost(report.total_cost_usd)}")
        return 0

    print(f"\nhalted: {report.detail}")
    print(f"total cost: {format_cost(report.total_cost_usd)}")
    return 1


def _edit_in_editor(text: str, suffix: str = ".md") -> str:
    """Hand text to $EDITOR and read back whatever comes out."""
    import subprocess
    import tempfile

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=suffix, delete=False) as handle:
        handle.write(text)
        path = handle.name
    try:
        subprocess.run([*editor.split(), path], check=True)
        return Path(path).read_text(encoding="utf-8")
    finally:
        Path(path).unlink(missing_ok=True)


def cmd_push(args: argparse.Namespace) -> int:
    from .push import Aborted, PushError, push, push_interactive, summarize
    from .review import TerminalReviewer

    repo = resolve_repo(args.repo)
    interactive = sys.stdin.isatty() and not args.yes and not args.dry_run

    prose = args.prose
    if not prose and not sys.stdin.isatty():
        prose = sys.stdin.read()
    if not prose and interactive:
        print("What do you want to achieve? (one line is fine)")
        prose = input("> ").strip()
    if not prose or not prose.strip():
        print("nothing to push: give prose as an argument or on stdin.", file=sys.stderr)
        return 2

    progress = for_terminal() if interactive else None

    try:
        linear = build_linear(repo, args.linear)
        if interactive:
            if progress:
                progress.start("reading the repository and drafting")
            tickets = push_interactive(
                prose=prose,
                linear=linear,
                reviewer=TerminalReviewer(ask=ask_after_agent, show=print),
                edit=_edit_in_editor,
                cwd=repo,
                on_activity=progress,
            )
        else:
            tickets = push(
                prose=prose, linear=linear, cwd=repo, dry_run=args.dry_run
            )
    except Aborted:
        print("nothing created.")
        return 0
    except MissingApiKey as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LinearApiError, PushError) as exc:
        print(f"push failed: {exc}", file=sys.stderr)
        return 2
    finally:
        # However this ended, the live line comes down before anything prints.
        if progress:
            progress.done()

    print(summarize(tickets))
    return 0


def _ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def _confirm(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def cmd_init(args: argparse.Namespace) -> int:
    """Walk someone through setting up credentials, and prove they work.

    Writes the per-user config rather than a per-repo one, because the key
    belongs to a person while the tool runs inside whichever repo you point it
    at. A key in one repo's .env is invisible from every other repo.
    """
    import getpass

    target = user_config_dir() / ENV_FILE
    existing = load_settings(None)

    print("Forman setup")
    print(f"Writing to {target}\n")

    if target.is_file() and not args.force:
        print(f"{target} already exists.")
        if not sys.stdin.isatty() or not _confirm("Overwrite it?", default=False):
            print("Nothing changed. Re-run with --force to overwrite.")
            return 0
        print()

    # -- key ----------------------------------------------------------------

    key = existing.api_key
    if key:
        print(f"Found an existing key ({mask(key)}).")
        if sys.stdin.isatty() and not _confirm("Keep using it?", default=True):
            key = None
        print()

    if not key:
        if not sys.stdin.isatty():
            sys.stdout.flush()
            print(
                "No API key, and nothing to prompt with. Get one at "
                "https://linear.app/settings/api and either export "
                f"{API_KEY_VAR} or write it to {target}.",
                file=sys.stderr,
            )
            return 2
        print("Get a personal API key: https://linear.app/settings/api")
        print("(Personal API keys, then Create key. It is shown only once.)")
        # getpass so the key never echoes and never lands in shell history.
        key = getpass.getpass("Paste it here (input hidden): ").strip()
        if not key:
            sys.stdout.flush()
            print("No key given, nothing written.", file=sys.stderr)
            return 2
        print()

    # -- verify -------------------------------------------------------------

    print("Checking the key against Linear...")
    client = GraphQLLinearClient(api_key=key)
    try:
        viewer = client.viewer()
        teams = client.teams()
    except LinearApiError as exc:
        sys.stdout.flush()
        print(f"\nThat key did not work: {exc}", file=sys.stderr)
        print("Nothing written. Run `forman init` again to retry.", file=sys.stderr)
        return 1
    print(f"  authenticated as {viewer.get('email') or viewer.get('name')}\n")

    values = {API_KEY_VAR: key}

    # -- team ---------------------------------------------------------------

    keys = [t["key"] for t in teams]
    if len(teams) == 1:
        print(f"One team visible ({keys[0]}), so no team setting needed.\n")
        team_key = keys[0]
    else:
        print(f"Teams visible: {', '.join(keys)}")
        print("Forman needs one to create issues on with `forman push`.")
        team_key = existing.team_key or keys[0]
        if sys.stdin.isatty():
            team_key = _ask("Which team key", default=team_key)
        values[TEAM_KEY_VAR] = team_key
        print()

    # -- review state -------------------------------------------------------

    client.team_key = team_key
    try:
        state = client.find_state(team_key, "in_review")
        print(f"Gate state: tickets will move to '{state['name']}' when a PR opens.\n")
    except LinearApiError:
        names = [s["name"] for s in client.team(team_key)["states"]["nodes"]]
        print(f"Team {team_key} has no state that looks like 'in review'.")
        print(f"  states: {', '.join(names)}")
        print(
            "Forman still opens the PR and comments the link; it just cannot\n"
            "move the ticket. Add an 'In Review' state under the Started group\n"
            "in Linear, or name an existing state to use instead."
        )
        if sys.stdin.isatty():
            chosen = _ask("Use which state (blank to skip)", default="")
            if chosen:
                values[REVIEW_STATE_VAR] = chosen
        print()

    # -- write --------------------------------------------------------------

    if existing.user:
        values[USER_VAR] = existing.user

    written = write_user_env(values, target)
    print(f"Wrote {written} (permissions 0600)\n")
    print("Next:")
    print("  forman doctor                  # confirm what Forman can see")
    print("  cd <your repo> && forman pull  # work your next ticket")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Prove the API key works and show what Forman can actually see.

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


def cmd_resume(args: argparse.Namespace) -> int:
    """Clear a halted run so `forman pull` will pick it back up.

    The loop never resets a failed or blocked sub-task by itself: both mean a
    human has to look at something. This command is that human saying they
    have, and it only ever moves work backwards to pending, never forwards.
    """
    from .state import reset_for_resume

    repo = resolve_repo(args.repo)
    store = StateStore(repo)

    if not store.exists(args.ticket):
        known = ", ".join(store.tickets()) or "(none)"
        print(
            f"no forman state for {args.ticket} in this repo. Known: {known}",
            file=sys.stderr,
        )
        return 2

    state = store.load(args.ticket)
    changed = reset_for_resume(state, include_blocked=not args.failed_only)

    if not changed:
        done = len(state.done_ids())
        print(f"{args.ticket}: nothing to reset ({done}/{len(state.subtasks)} done).")
        if state.all_done():
            print("Everything finished. Run `forman pull` to open the PR.")
        return 0

    store.save(state)
    for subtask_id, was in changed:
        print(f"  {subtask_id}: {was} -> pending")
    print(f"\n{args.ticket}: {len(changed)} sub-task(s) reset. Work already done is kept.")

    status = git_ops.worktree_status(repo)
    if not status.clean:
        sys.stdout.flush()  # stderr is unbuffered and would jump the queue
        print(
            "\nThe working tree is dirty, so `forman pull` will refuse to start:",
            file=sys.stderr,
        )
        for path in status.files[:10]:
            print(f"  {path}", file=sys.stderr)
        print(
            "\nA halted sub-task often leaves useful partial work. Read it, then "
            "either commit it or discard it before resuming.",
            file=sys.stderr,
        )
        return 1

    print(f"\nNext:  forman pull --ticket {args.ticket}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    store = StateStore(repo)
    tickets = store.tickets()
    if not tickets:
        print("no forman state in this repo yet.")
        return 0
    for ticket in tickets:
        state = store.load(ticket)
        done = len(state.done_ids())
        print(f"{state.ticket}  {state.status}  {done}/{len(state.subtasks)}  {state.branch}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forman", description=__doc__)
    parser.add_argument(
        "--repo", default=None, help="target repo (defaults to the current directory)"
    )
    parser.add_argument(
        "--linear",
        choices=["graphql", "stub"],
        default="graphql",
        help="Linear backend. `graphql` (default) uses LINEAR_API_KEY against "
        "the real API. `stub` is offline and needs no account.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pull = sub.add_parser("pull", help="pull one ticket and take it to the gate")
    p_pull.add_argument(
        "--ticket",
        default=None,
        metavar="ID",
        help="work this exact ticket (e.g. TEAM-42) instead of letting Forman "
        "choose. Skips the readiness checks.",
    )
    p_pull.set_defaults(func=cmd_pull)

    p_push = sub.add_parser("push", help="turn prose into Linear tickets")
    p_push.add_argument("prose", nargs="?", help="issue description (or pipe it on stdin)")
    p_push.add_argument(
        "--dry-run", action="store_true", help="show the tickets without creating them"
    )
    p_push.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the conversation and the draft review; file it in one shot",
    )
    p_push.set_defaults(func=cmd_push)

    p_status = sub.add_parser("status", help="show forman state in this repo")
    p_status.set_defaults(func=cmd_status)

    p_init = sub.add_parser(
        "init", help="set up your Linear credentials and check they work"
    )
    p_init.add_argument(
        "--force", action="store_true", help="overwrite an existing config without asking"
    )
    p_init.set_defaults(func=cmd_init)

    p_resume = sub.add_parser(
        "resume", help="reset a halted ticket's failed/blocked sub-tasks to pending"
    )
    p_resume.add_argument("ticket", help="ticket identifier, e.g. TEAM-42")
    p_resume.add_argument(
        "--failed-only",
        action="store_true",
        help="reset only failed sub-tasks, leaving blocked ones alone",
    )
    p_resume.set_defaults(func=cmd_resume)

    p_doctor = sub.add_parser(
        "doctor", help="check the Linear API key and show what Forman can see"
    )
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
