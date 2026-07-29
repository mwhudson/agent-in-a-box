# Tests for aiab.release — release normalisation and template-container naming.

import datetime

import pytest

from aiab import release


# A stand-in for /usr/share/distro-info/ubuntu.csv: same columns, an EOL
# release, a supported LTS (version carrying the " LTS" suffix the real file
# uses), an unreleased series, and a not-yet-opened one after it.
CSV = """\
version,codename,series,created,release,eol,eol-server,eol-esm
4.10,Warty Warthog,warty,2004-03-05,2004-10-20,2006-04-30
39.10,Expired Emu,expired,2039-04-26,2039-10-13,2040-07-10
40.04 LTS,Fictional Ferret,fictional,2039-10-17,2040-04-25,2045-04-25
40.10,Notional Numbat,notional,2040-04-26,2040-10-15,2041-07-15
41.04,Unopened Urchin,unopened,2040-10-16,2041-04-22,2042-01-22
"""


@pytest.fixture
def distro_info(tmp_path, monkeypatch):
    """Point release at the fixture csv above."""
    path = tmp_path / "ubuntu.csv"
    path.write_text(CSV)
    monkeypatch.setattr(release, "DISTRO_INFO_CSV", path)
    return path


@pytest.fixture
def no_distro_info(tmp_path, monkeypatch):
    """Point release at a csv that isn't there, as on a host without it."""
    monkeypatch.setattr(release, "DISTRO_INFO_CSV", tmp_path / "absent.csv")


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


def test_normalize_uses_distro_info_codenames(distro_info):
    # A codename the built-in table has never heard of, resolved from the csv
    # — and the " LTS" the version column carries is stripped.
    assert release.normalize("fictional") == "40.04"


def test_normalize_falls_back_to_table_without_distro_info(no_distro_info):
    # No csv: the built-in table still answers, so aiab works on a host
    # without distro-info-data.
    assert release.normalize("jammy") == "22.04"
    with pytest.raises(ValueError):
        release.normalize("fictional")


def test_normalize_devel(distro_info, monkeypatch):
    # 'devel' resolves to a fixed version, so what's stored can't drift when
    # that release ships.
    monkeypatch.setattr(release, "devel_version", lambda: "40.10")
    assert release.normalize("devel") == "40.10"


def test_normalize_devel_unresolvable_explains(no_distro_info):
    with pytest.raises(ValueError, match="distro-info-data"):
        release.normalize("devel")


# ---------------------------------------------------------------------------
# devel_version
# ---------------------------------------------------------------------------


def test_devel_is_the_opened_but_unreleased_series(distro_info):
    # Between notional's opening and its release, notional is devel: the
    # released LTS before it is out, and unopened hasn't started.
    assert release.devel_version(datetime.date(2040, 7, 1)) == "40.10"


def test_devel_moves_on_at_release(distro_info):
    # The day after notional releases, unopened is open and devel.
    assert release.devel_version(datetime.date(2040, 10, 17)) == "41.04"


def test_devel_none_without_distro_info(no_distro_info):
    assert release.devel_version(datetime.date(2040, 7, 1)) is None


def test_distro_info_rows_skip_pre_version_scheme_releases(distro_info):
    # 4.10 isn't YY.MM; dropping it keeps the version regex the single
    # definition of what a base looks like.
    assert "warty" not in {row.series for row in release._distro_info_rows()}


# ---------------------------------------------------------------------------
# supported
# ---------------------------------------------------------------------------


def test_supported_drops_eol_and_unopened(distro_info):
    # On this date expired is past its eol and unopened hasn't been created,
    # leaving the two releases you'd actually build a container on.
    assert release.supported(datetime.date(2040, 7, 15)) == [
        ("40.04", "fictional"),
        ("40.10", "notional"),
    ]


def test_supported_includes_devel(distro_info):
    # The in-development release has no eol yet in any practical sense, and
    # is a legitimate base — 'aiab base devel' names it.
    assert ("41.04", "unopened") in release.supported(datetime.date(2040, 10, 20))


def test_supported_falls_back_to_table(no_distro_info):
    # Without the csv there are no dates to filter on, so the whole built-in
    # table is offered rather than nothing.
    assert release.supported(datetime.date(2040, 7, 15)) == sorted(
        (v, c) for c, v in release.CODENAMES.items()
    )


# ---------------------------------------------------------------------------
# image_for
# ---------------------------------------------------------------------------


def test_image_for():
    assert release.image_for("24.04") == "ubuntu-daily:24.04"


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

# Synthetic: "two-words" stands in for an agent name containing a hyphen,
# which must not confuse the '<agent>-base-<token>' pattern.
AGENTS = ("claude", "two-words", "opencode", "copilot")


def test_is_base_recognises_default_and_alternate():
    assert release.is_base_container_name("claude", AGENTS)
    assert release.is_base_container_name("claude-base-2204", AGENTS)
    assert release.is_base_container_name("two-words-base-2404", AGENTS)


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
