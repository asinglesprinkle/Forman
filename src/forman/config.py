"""Configuration and secrets.

The Linear API key is per-person, not per-repo, but Forman runs from inside
whichever repo you point it at. So the key is looked up in this order:

  1. the environment (LINEAR_API_KEY)
  2. `.env` in the target repo
  3. `~/.config/forman/.env`

Put it in (3) once and every repo you run from picks it up. Use (2) when a
particular repo needs a different key, for example a work board and a personal
one under different accounts.

No dependency on python-dotenv: the parser below handles the subset of .env
syntax that matters and nothing else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = ".env"
API_KEY_VAR = "LINEAR_API_KEY"
TEAM_KEY_VAR = "LINEAR_TEAM_KEY"
REVIEW_STATE_VAR = "LINEAR_REVIEW_STATE"
USER_VAR = "LINEAR_USER"


class MissingApiKey(RuntimeError):
    """No Linear API key anywhere we look."""

    def __init__(self, searched: list[Path]) -> None:
        self.searched = searched
        where = "\n".join(f"  - {p}" for p in searched)
        super().__init__(
            f"no {API_KEY_VAR} found.\n\n"
            f"Get a key at https://linear.app/settings/api (Personal API keys, "
            f"Create key), then put it in one of:\n{where}\n\n"
            f"as a line like:\n\n    {API_KEY_VAR}=lin_api_xxxxx\n\n"
            f"or export it in your shell. To run without Linear at all, use "
            f"`--linear stub`."
        )


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "forman"


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=value lines. Ignores comments, blanks, and a leading `export`.

    Strips one layer of matching quotes so a key pasted with quotes still works.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def env_files(repo_root: str | Path | None = None) -> list[Path]:
    """Where we look for a .env, nearest first."""
    paths = []
    if repo_root is not None:
        paths.append(Path(repo_root) / ENV_FILE)
    paths.append(user_config_dir() / ENV_FILE)
    return paths


def load_env(repo_root: str | Path | None = None) -> dict[str, str]:
    """Merge the real environment over any .env files found.

    A value already exported in the shell always wins, so a one-off
    `LINEAR_API_KEY=... forman pull` overrides the file without editing it.
    """
    values: dict[str, str] = {}
    for path in reversed(env_files(repo_root)):  # nearest file wins
        if path.is_file():
            values.update(parse_env(path.read_text(encoding="utf-8")))
    values.update({k: v for k, v in os.environ.items() if k in _KNOWN_VARS})
    return values


_KNOWN_VARS = {API_KEY_VAR, TEAM_KEY_VAR, REVIEW_STATE_VAR, USER_VAR}


@dataclass
class Settings:
    api_key: str | None = None
    team_key: str | None = None
    review_state: str | None = None
    user: str | None = None

    def require_api_key(self, repo_root: str | Path | None = None) -> str:
        if not self.api_key:
            raise MissingApiKey(env_files(repo_root))
        return self.api_key


def mask(secret: str) -> str:
    """Show enough of a key to recognise it, never enough to use it."""
    if len(secret) <= 12:
        return "*" * len(secret)
    return f"{secret[:8]}...{secret[-4:]}"


def render_env(values: dict[str, str]) -> str:
    """Render a .env file. Only known variables, only non-empty ones."""
    lines = [
        "# Written by `forman init`. Safe to edit by hand.",
        "# Docs: https://linear.app/settings/api for the API key.",
        "",
    ]
    for var in (API_KEY_VAR, TEAM_KEY_VAR, REVIEW_STATE_VAR, USER_VAR):
        value = values.get(var)
        if value:
            lines.append(f"{var}={value}")
    return "\n".join(lines) + "\n"


def write_user_env(values: dict[str, str], path: str | Path | None = None) -> Path:
    """Write the per-user .env, readable only by its owner.

    0600 matters: this file holds a credential that can read and modify every
    issue the key's owner can see.
    """
    target = Path(path) if path else user_config_dir() / ENV_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_env(values), encoding="utf-8")
    target.chmod(0o600)
    return target


def load_settings(repo_root: str | Path | None = None) -> Settings:
    values = load_env(repo_root)
    return Settings(
        api_key=values.get(API_KEY_VAR) or None,
        team_key=values.get(TEAM_KEY_VAR) or None,
        review_state=values.get(REVIEW_STATE_VAR) or None,
        user=values.get(USER_VAR) or None,
    )
