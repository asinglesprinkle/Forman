"""Every git and gh side effect, behind one function each.

This mirrors the flow a human already follows by hand: check out the default
branch, pull, make sure the tree is clean, then cut a branch named after the
ticket. Nothing here decides anything; the orchestrator does. Each function
takes plain arguments and returns a structured value, which keeps the decisions
in one place and lets the loop above be exercised against a fake that never
shells out.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import CommitResult, PullRequest

# Branches look like `<team-key>-<number>/<title-slug>`, lowercased. The team
# key and number come from whatever Linear returns; nothing is hardcoded.
MAX_SLUG_LEN = 50


class GitError(RuntimeError):
    def __init__(self, args: list[str], code: int, stderr: str) -> None:
        self.args_run = args
        self.code = code
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args)} failed ({code}): {self.stderr}")


class DirtyWorktree(RuntimeError):
    """The target repo has uncommitted changes. We abort rather than stash:
    silently moving someone's work is worse than refusing to start."""

    def __init__(self, files: list[str]) -> None:
        self.files = files
        listed = ", ".join(files[:10]) + ("..." if len(files) > 10 else "")
        super().__init__(f"working tree is not clean: {listed}")


@dataclass
class WorktreeStatus:
    clean: bool
    files: list[str]


def _run(repo: str | Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc.stdout.strip()


# -- repo discovery ----------------------------------------------------------


def is_repo(path: str | Path) -> bool:
    try:
        return _run(path, "rev-parse", "--is-inside-work-tree") == "true"
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


def repo_root(path: str | Path) -> Path:
    return Path(_run(path, "rev-parse", "--show-toplevel")).resolve()


def has_remote(repo: str | Path, name: str = "origin") -> bool:
    return name in _run(repo, "remote").splitlines()


# -- inspection --------------------------------------------------------------


def current_branch(repo: str | Path) -> str:
    return _run(repo, "rev-parse", "--abbrev-ref", "HEAD")


def worktree_status(repo: str | Path) -> WorktreeStatus:
    out = _run(repo, "status", "--porcelain")
    # Porcelain lines are `XY path`, but _run strips the whole output, which
    # eats the leading space of the first line when its index status is blank.
    # Slicing past the two status columns and stripping handles both shapes.
    files = [line[2:].strip() for line in out.splitlines() if line.strip()]
    return WorktreeStatus(clean=not files, files=[f for f in files if f])


def require_clean(repo: str | Path) -> None:
    st = worktree_status(repo)
    if not st.clean:
        raise DirtyWorktree(st.files)


def default_branch(repo: str | Path) -> str:
    """Detect the default branch. Never assume `main`.

    Order: what origin says it is, then the usual local names, then whatever is
    currently checked out.
    """
    if has_remote(repo):
        ref = _run(
            repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False
        )
        if ref:
            return ref.rsplit("/", 1)[-1]
    for candidate in ("main", "master", "trunk", "develop"):
        if _run(repo, "rev-parse", "--verify", "--quiet", candidate, check=False):
            return candidate
    return current_branch(repo)


def branch_exists(repo: str | Path, name: str) -> bool:
    return bool(_run(repo, "rev-parse", "--verify", "--quiet", name, check=False))


# -- branch naming -----------------------------------------------------------


def slugify(text: str, max_len: int = MAX_SLUG_LEN) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) <= max_len:
        return slug or "work"
    cut = slug[:max_len]
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]  # do not end mid-word
    return cut.strip("-") or slug[:max_len].strip("-")


def branch_name(identifier: str, title: str) -> str:
    """`<TEAM_KEY>-<NUMBER>` + a title -> `<team-key>-<number>/<title-slug>`.

    For example `TEAM-42` + "Add rate limiting to the auth endpoint" becomes
    `team-42/add-rate-limiting-to-the-auth-endpoint`. The key and number come
    straight from whatever Linear returns; nothing about them is hardcoded.
    """
    return f"{identifier.lower()}/{slugify(title)}"


# -- mutation ----------------------------------------------------------------


def sync_default_branch(repo: str | Path) -> str:
    """Check out the default branch and fast-forward it. Returns the branch name.

    Requires a clean tree; call require_clean first (the orchestrator does).
    A repo with no remote is fine: there is simply nothing to pull.
    """
    base = default_branch(repo)
    _run(repo, "checkout", base)
    if has_remote(repo):
        _run(repo, "pull", "--ff-only")
    return base


def create_branch(repo: str | Path, name: str) -> str:
    """Create the ticket branch, or switch to it if a previous run made it.

    Re-runs are expected: the human resolves a blocker and runs forman again.
    """
    if branch_exists(repo, name):
        _run(repo, "checkout", name)
    else:
        _run(repo, "checkout", "-b", name)
    return name


def ensure_ignored(repo: str | Path, entry: str = ".forman/") -> bool:
    """Keep Forman's bookkeeping out of the target repo's commits.

    Written to `.git/info/exclude` rather than `.gitignore` on purpose: this is
    our tooling's business, not a change to the project's tracked files.
    Returns True when the entry was added.
    """
    exclude = Path(_run(repo, "rev-parse", "--git-dir"))
    if not exclude.is_absolute():
        exclude = Path(repo) / exclude
    exclude = exclude / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)

    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if entry in existing.split():
        return False
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    exclude.write_text(
        existing + prefix + f"# added by forman\n{entry}\n", encoding="utf-8"
    )
    return True


def commit_all(repo: str | Path, message: str) -> CommitResult:
    """Stage everything and commit. A no-op when there is nothing to commit.

    Called after each done sub-task so a mid-run failure leaves a clean,
    inspectable tree rather than one giant uncommitted blob.
    """
    _run(repo, "add", "-A")
    if not _run(repo, "status", "--porcelain"):
        return CommitResult(created=False, message=message)
    _run(repo, "commit", "-m", message)
    return CommitResult(
        created=True, sha=_run(repo, "rev-parse", "HEAD"), message=message
    )


def push_branch(repo: str | Path, branch: str) -> bool:
    if not has_remote(repo):
        return False
    _run(repo, "push", "-u", "origin", branch)
    return True


# -- pull request ------------------------------------------------------------


def gh_available() -> bool:
    return shutil.which("gh") is not None


def open_pull_request(
    repo: str | Path, branch: str, base: str, title: str, body: str
) -> PullRequest:
    """Open a PR with gh if it is available, otherwise hand the human the body.

    Never auto-merges. This is the gate.
    """
    if not gh_available() or not has_remote(repo):
        return PullRequest(url=None, branch=branch, title=title, body=body, manual=True)

    proc = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # An existing PR for this branch is the common case on a re-run.
        existing = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return PullRequest(
                url=existing.stdout.strip(), branch=branch, title=title, body=body
            )
        return PullRequest(url=None, branch=branch, title=title, body=body, manual=True)

    url = next(
        (
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip().startswith("http")
        ),
        None,
    )
    return PullRequest(
        url=url, branch=branch, title=title, body=body, manual=url is None
    )
