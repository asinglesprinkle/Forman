"""What a session is allowed to do, asserted rather than assumed.

These are the settings that decide whether an agent does what its contract says
or whatever the machine it runs on permits. They were all wrong once: a
ticket-drafting agent told it could use Read/Grep/Glob ran `bash cat` on files
outside the repository, and reached a live Linear workspace over an MCP server
it inherited from the user's own config - straight past the human gate the whole
pipeline is built around.

Nothing here starts a session. `_options` is pure: it takes the options class
and returns an instance, so a stand-in captures exactly what the SDK would have
been handed.
"""

from __future__ import annotations

import pytest

from forman.spawn import DEFAULT_TOOLS, READ_ONLY_TOOLS, _options


class FakeOptions:
    """Stands in for ClaudeAgentOptions and records what it was built with."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def built(**rest) -> dict:
    return _options(FakeOptions, tools=READ_ONLY_TOOLS, **rest).kwargs


# -- the toolset ---------------------------------------------------------------


def test_the_tool_list_is_set_as_the_base_toolset_not_only_as_permissions():
    """`allowed_tools` alone only skips permission prompts - the model keeps
    every built-in tool. `tools` is what actually removes them."""
    assert built()["tools"] == READ_ONLY_TOOLS


def test_a_read_only_session_is_not_handed_bash():
    assert "Bash" not in built()["tools"]
    assert "Bash" not in built()["allowed_tools"]


def test_a_sub_task_session_still_gets_what_it_needs_to_work():
    """Sub-tasks run tests and commit; narrowing them to reads would break the
    pipeline. The lockdown is per-call, not a blanket ban."""
    tools = _options(FakeOptions, tools=DEFAULT_TOOLS).kwargs["tools"]

    assert "Bash" in tools and "Edit" in tools


def test_the_caller_cannot_be_narrowed_by_a_later_mutation():
    """The list is copied, so a caller holding DEFAULT_TOOLS cannot have the
    session's toolset change under it."""
    mine = ["Read"]
    options = _options(FakeOptions, tools=mine)
    mine.append("Bash")

    assert options.kwargs["tools"] == ["Read"]


# -- what the session cannot reach ---------------------------------------------


def test_no_mcp_servers_are_configured():
    assert built()["mcp_servers"] == {}


def test_the_user_s_own_mcp_config_is_ignored():
    """Without this the SDK loads project .mcp.json and user settings, which is
    how a planning agent ended up holding write access to a Linear workspace."""
    assert built()["strict_mcp_config"] is True


def test_no_filesystem_settings_are_loaded():
    assert built()["setting_sources"] == []


# -- the shape of the call -----------------------------------------------------


def test_the_caller_s_own_arguments_are_passed_through():
    kwargs = built(system_prompt="be brief", cwd="/repo", model="m", max_turns=7)

    assert kwargs["system_prompt"] == "be brief"
    assert kwargs["cwd"] == "/repo"
    assert kwargs["model"] == "m"
    assert kwargs["max_turns"] == 7


def test_an_sdk_that_does_not_know_these_fields_fails_loudly():
    """Constructed directly rather than filtered: an SDK too old to accept the
    restrictions must raise, not quietly run an unrestricted agent."""

    class OldOptions:
        def __init__(self, *, system_prompt=None, allowed_tools=None, **_):
            raise TypeError("unexpected keyword argument 'strict_mcp_config'")

    with pytest.raises(TypeError):
        _options(OldOptions, tools=READ_ONLY_TOOLS)
