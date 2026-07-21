# Tests for aiab.profiles — profile lookup, agent scoping, and the naming
# rules that decide when a profile forks an agent's identity.
#
# The user-profile half reads through aiab.state, so _PROFILES_PATH is
# redirected to a tmp dir; no real state is read or written.

import pytest

from aiab import profiles
import aiab.state as state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_PROFILES_PATH", tmp_path / "profiles.json")


# ---------------------------------------------------------------------------
# valid_name
# ---------------------------------------------------------------------------


def test_valid_name_accepts_agent_shaped_names():
    assert profiles.valid_name("openrouter")
    assert profiles.valid_name("no-net")
    assert profiles.valid_name("py3")


def test_valid_name_rejects_container_hostile_names():
    # These would end up inside an LXD instance name, which is a hostname.
    for bad in ("Open Router", "open_router", "-leading", "with.dot", ""):
        assert not profiles.valid_name(bad), bad


# ---------------------------------------------------------------------------
# get / names
# ---------------------------------------------------------------------------


def test_get_returns_builtin():
    assert profiles.get("openrouter") == profiles.BUILTIN["openrouter"]


def test_get_unknown_is_none():
    assert profiles.get("nope") is None


def test_get_returns_user_profile():
    state.set_profile("hardened", {"allow": ["example.com"]})
    assert profiles.get("hardened") == {"allow": ["example.com"]}


def test_names_merges_builtin_and_user():
    state.set_profile("hardened", {})
    assert profiles.names() == ["hardened", "openrouter"]


def test_names_is_just_builtins_when_nothing_recorded():
    assert profiles.names() == ["openrouter"]


# ---------------------------------------------------------------------------
# applies_to
# ---------------------------------------------------------------------------


def test_applies_to_honours_agent_list():
    assert profiles.applies_to({"agents": ["claude"]}, "claude")
    assert not profiles.applies_to({"agents": ["claude"]}, "copilot")


def test_applies_to_any_agent_when_unscoped():
    # No 'agents' key, and an empty list, both mean "any agent".
    assert profiles.applies_to({}, "copilot")
    assert profiles.applies_to({"agents": []}, "copilot")


def test_openrouter_is_claude_only():
    assert profiles.applies_to(profiles.BUILTIN["openrouter"], "claude")
    assert not profiles.applies_to(profiles.BUILTIN["openrouter"], "opencode")


# ---------------------------------------------------------------------------
# home_key / container_prefix
# ---------------------------------------------------------------------------


def test_no_profile_keeps_the_bare_agent_names():
    assert profiles.home_key("claude", None, False) == "claude"
    assert profiles.container_prefix("claude", None, False) == "claude"


def test_isolated_profile_forks_both_names():
    assert profiles.home_key("claude", "openrouter", True) == "claude@openrouter"
    assert (
        profiles.container_prefix("claude", "openrouter", True) == "claude-openrouter"
    )


def test_non_isolated_profile_shares_the_agent_identity():
    # A profile that only layers settings reuses the agent's credential store
    # and session container — entering it must not mean re-authenticating.
    assert profiles.home_key("claude", "hardened", False) == "claude"
    assert profiles.container_prefix("claude", "hardened", False) == "claude"


def test_container_prefix_avoids_the_at_sign():
    # LXD instance names are hostnames; '@' is fine in the home dir but not here.
    assert "@" not in profiles.container_prefix("claude", "openrouter", True)
