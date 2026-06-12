# Tests for aiab.release — release normalisation and template-container naming.

import pytest

from aiab import release


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_version_passthrough():
    assert release.normalize("22.04") == "22.04"


def test_normalize_codename():
    assert release.normalize("jammy") == "22.04"
    assert release.normalize("noble") == "24.04"


def test_normalize_is_case_and_space_insensitive():
    assert release.normalize("  Jammy ") == "22.04"


def test_normalize_unknown_version_still_accepted():
    # A version we don't carry a codename for is taken at face value, so a new
    # release works before the codename table is updated.
    assert release.normalize("30.04") == "30.04"


def test_normalize_rejects_garbage():
    with pytest.raises(ValueError):
        release.normalize("bananas")


# ---------------------------------------------------------------------------
# image_for
# ---------------------------------------------------------------------------


def test_image_for():
    assert release.image_for("24.04") == "ubuntu:24.04"


# ---------------------------------------------------------------------------
# base_container_name
# ---------------------------------------------------------------------------


def test_base_name_default_is_bare_agent():
    assert release.base_container_name("claude", release.DEFAULT_BASE) == "claude"


def test_base_name_alternate_gets_token_suffix():
    assert release.base_container_name("claude", "22.04") == "claude-base-2204"


# ---------------------------------------------------------------------------
# is_base_container_name
# ---------------------------------------------------------------------------

AGENTS = ("claude", "claude-or", "opencode", "copilot")


def test_is_base_recognises_default_and_alternate():
    assert release.is_base_container_name("claude", AGENTS)
    assert release.is_base_container_name("claude-base-2204", AGENTS)
    assert release.is_base_container_name("claude-or-base-2404", AGENTS)


def test_is_base_rejects_session_names():
    # Session containers are '<agent>-<basename>-<8 hex hash>'.
    assert not release.is_base_container_name("claude-myproj-1a2b3c4d", AGENTS)
    # Even a directory literally named "base" with an all-digit hash: the hash
    # is eight chars, the base token is four, so no false positive.
    assert not release.is_base_container_name("claude-base-12345678", AGENTS)


# ---------------------------------------------------------------------------
# base_names_for_agent
# ---------------------------------------------------------------------------


def test_base_names_for_agent_default_first():
    instances = {
        "claude": "STOPPED",
        "claude-base-2204": "STOPPED",
        "claude-myproj-1a2b3c4d": "RUNNING",
        "opencode": "STOPPED",
    }
    assert release.base_names_for_agent("claude", instances) == [
        "claude",
        "claude-base-2204",
    ]


def test_base_names_for_agent_only_alternate():
    instances = {"claude-base-2204": "STOPPED"}
    assert release.base_names_for_agent("claude", instances) == ["claude-base-2204"]
