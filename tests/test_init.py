"""`forman init` writes the per-user config, and writes it safely."""

import stat

import pytest

from forman.config import (
    API_KEY_VAR,
    REVIEW_STATE_VAR,
    TEAM_KEY_VAR,
    load_settings,
    mask,
    parse_env,
    render_env,
    write_user_env,
)


def test_mask_shows_enough_to_recognise_not_enough_to_use():
    masked = mask("lin_api_abcdefghijklmnop1234")
    assert masked.startswith("lin_api_")
    assert masked.endswith("1234")
    assert "abcdefghijklmnop" not in masked


def test_mask_hides_short_secrets_entirely():
    assert set(mask("short")) == {"*"}


def test_render_env_skips_empty_and_unknown_values():
    out = render_env(
        {
            API_KEY_VAR: "lin_api_x",
            TEAM_KEY_VAR: "",
            "SOMETHING_ELSE": "nope",
        }
    )
    assert f"{API_KEY_VAR}=lin_api_x" in out
    assert TEAM_KEY_VAR not in out
    assert "SOMETHING_ELSE" not in out


def test_render_env_round_trips_through_the_parser():
    values = {
        API_KEY_VAR: "lin_api_x",
        TEAM_KEY_VAR: "TEAM",
        REVIEW_STATE_VAR: "Awaiting Review",
    }
    assert parse_env(render_env(values)) == values


def test_written_config_is_owner_only(tmp_path):
    target = tmp_path / "nested" / ".env"
    written = write_user_env({API_KEY_VAR: "lin_api_secret"}, target)

    mode = stat.S_IMODE(written.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # Nobody but the owner: this file can read and modify every issue the key
    # can reach.
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_written_config_is_what_load_settings_reads_back(tmp_path, monkeypatch):
    monkeypatch.delenv(API_KEY_VAR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    write_user_env({API_KEY_VAR: "lin_api_x", TEAM_KEY_VAR: "TEAM"})

    settings = load_settings(tmp_path / "some-repo")
    assert settings.api_key == "lin_api_x"
    assert settings.team_key == "TEAM"


def test_init_lands_where_every_repo_can_see_it(tmp_path, monkeypatch):
    """The whole point of init: a key written once works from anywhere.

    A key in one repo's .env is invisible from every other repo, which is the
    trap this command exists to keep people out of.
    """
    monkeypatch.delenv(API_KEY_VAR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    write_user_env({API_KEY_VAR: "lin_api_x"})

    for repo in ("repo-a", "repo-b", "somewhere/else"):
        assert load_settings(tmp_path / repo).api_key == "lin_api_x"


def test_a_repo_env_still_wins_for_that_repo(tmp_path, monkeypatch):
    monkeypatch.delenv(API_KEY_VAR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    write_user_env({API_KEY_VAR: "lin_api_user"})

    repo = tmp_path / "special-repo"
    repo.mkdir()
    (repo / ".env").write_text(f"{API_KEY_VAR}=lin_api_repo\n")

    assert load_settings(repo).api_key == "lin_api_repo"
    assert load_settings(tmp_path / "other").api_key == "lin_api_user"


@pytest.mark.parametrize("value", ["lin_api_x", "lin_api_with=equals", "with spaces"])
def test_values_survive_a_write_and_read_cycle(tmp_path, value):
    target = tmp_path / ".env"
    write_user_env({API_KEY_VAR: value}, target)
    assert parse_env(target.read_text())[API_KEY_VAR] == value
