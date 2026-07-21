# Tests for the pure helper functions in aiab.cli.
#
# Only the host-side filesystem helpers are covered here; the parts that drive
# LXD (containers, devices) need a live LXD and are exercised manually.

from pathlib import Path
from typing import Any

import pytest

from aiab import CONTAINER_HOME
from aiab import agents, profiles, state
from aiab.cli import (
    _guard_git_repo,
    _json_set,
    _json_unset,
    _parse_config_value,
    _reseed_file,
    _reseed_tree,
    _resolve_profile,
    _session_env,
)


class FakeContainer:
    """Records add_config_overlay calls instead of touching LXD."""

    def __init__(self):
        self.overlays = []

    def add_config_overlay(
        self, host_path, container_path, container_user=0, readonly=False
    ):
        self.overlays.append((host_path, container_path, readonly))


# ---------------------------------------------------------------------------
# _reseed_file
# ---------------------------------------------------------------------------


def test_reseed_file_creates_copy(tmp_path):
    src = tmp_path / "src"
    src.write_text("hello\n")
    dst = tmp_path / "sub" / "dst"
    _reseed_file(dst, src)
    assert dst.read_text() == "hello\n"


def test_reseed_file_overwrites_in_place(tmp_path):
    # In-place (same inode) matters: a sidecar already bind-mounted into a
    # running container must reflect the reseeded contents.
    src = tmp_path / "src"
    src.write_text("new\n")
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    inode_before = dst.stat().st_ino
    _reseed_file(dst, src)
    assert dst.read_text() == "new\n"
    assert dst.stat().st_ino == inode_before


# ---------------------------------------------------------------------------
# _reseed_tree
# ---------------------------------------------------------------------------


def test_reseed_tree_mirrors_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "pre-commit").write_text("#!/bin/sh\n")
    (src / "nested").mkdir()
    (src / "nested" / "f").write_text("x\n")

    dst = tmp_path / "dst"
    _reseed_tree(dst, src)

    assert (dst / "pre-commit").read_text() == "#!/bin/sh\n"
    assert (dst / "nested" / "f").read_text() == "x\n"


def test_reseed_tree_clears_existing_entries(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep").write_text("keep\n")

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "stale").write_text("stale\n")
    (dst / "stale-dir").mkdir()

    _reseed_tree(dst, src)

    assert (dst / "keep").read_text() == "keep\n"
    assert not (dst / "stale").exists()
    assert not (dst / "stale-dir").exists()


def test_reseed_tree_preserves_dst_inode(tmp_path):
    # The directory itself is preserved (only entries replaced) so a live bind
    # mount of it keeps working across a reseed.
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    inode_before = dst.stat().st_ino

    _reseed_tree(dst, src)

    assert dst.stat().st_ino == inode_before


# ---------------------------------------------------------------------------
# _guard_git_repo
# ---------------------------------------------------------------------------


def _make_repo(root, *, hooks=True, config=True):
    """Build a minimal repo dir with a real .git directory under root."""
    git = root / ".git"
    git.mkdir(parents=True)
    if hooks:
        (git / "hooks").mkdir()
        (git / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
    if config:
        (git / "config").write_text("[core]\n")
    return root


def test_guard_git_repo_overlays_hooks_and_config(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    guard = tmp_path / "guard"
    container: Any = FakeContainer()

    _guard_git_repo(container, repo, "/work/repo", guard)

    # The sidecars are seeded from the host repo...
    assert (guard / "hooks" / "pre-commit").read_text() == "#!/bin/sh\n"
    assert (guard / "config").read_text() == "[core]\n"
    # ...and overlaid at the repo's container .git paths (config read-only).
    paths = {cpath: ro for _host, cpath, ro in container.overlays}
    assert paths == {
        "/work/repo/.git/hooks": False,
        "/work/repo/.git/config": True,
    }


def test_guard_git_repo_noop_without_git(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    container: Any = FakeContainer()

    _guard_git_repo(container, repo, "/work/plain", tmp_path / "guard")

    assert container.overlays == []


def test_guard_git_repo_noop_for_gitfile(tmp_path):
    # A linked worktree / submodule checkout has a .git *file*, not a dir; its
    # real hooks/config live elsewhere, so we skip rather than guard the wrong
    # thing.
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    container: Any = FakeContainer()

    _guard_git_repo(container, repo, "/work/wt", tmp_path / "guard")

    assert container.overlays == []


def test_guard_git_repo_handles_missing_hooks_or_config(tmp_path):
    # Only what exists is overlaid: a repo with config but no hooks dir.
    repo = _make_repo(tmp_path / "repo", hooks=False)
    container: Any = FakeContainer()

    _guard_git_repo(container, repo, "/work/repo", tmp_path / "guard")

    cpaths = [cpath for _host, cpath, _ro in container.overlays]
    assert cpaths == ["/work/repo/.git/config"]


# ---------------------------------------------------------------------------
# _json_set / _json_unset / _parse_config_value (aiab opencode config)
# ---------------------------------------------------------------------------


def test_json_set_nested_creates_path():
    data: dict = {}
    _json_set(data, "provider.openrouter.options.apiKey", "sk-or-x")
    assert data == {"provider": {"openrouter": {"options": {"apiKey": "sk-or-x"}}}}


def test_json_set_top_level():
    data: dict = {}
    _json_set(data, "model", "anthropic/claude-sonnet-4-6")
    assert data == {"model": "anthropic/claude-sonnet-4-6"}


def test_json_set_preserves_siblings():
    data = {"provider": {"openrouter": {"options": {"apiKey": "old"}}}}
    _json_set(data, "provider.openrouter.options.baseURL", "https://x")
    assert data["provider"]["openrouter"]["options"] == {
        "apiKey": "old",
        "baseURL": "https://x",
    }


def test_json_set_overwrites_non_dict_intermediate():
    data = {"provider": "scalar"}
    _json_set(data, "provider.openrouter.apiKey", "k")
    assert data == {"provider": {"openrouter": {"apiKey": "k"}}}


def test_json_unset_removes_and_prunes_empty_parents():
    data = {"provider": {"openrouter": {"options": {"apiKey": "k"}}}}
    assert _json_unset(data, "provider.openrouter.options.apiKey") is True
    # The whole now-empty chain is pruned.
    assert data == {}


def test_json_unset_keeps_non_empty_parents():
    data = {"provider": {"openrouter": {"options": {"apiKey": "k", "x": "y"}}}}
    assert _json_unset(data, "provider.openrouter.options.apiKey") is True
    assert data == {"provider": {"openrouter": {"options": {"x": "y"}}}}


def test_json_unset_absent_key():
    data = {"model": "m"}
    assert _json_unset(data, "provider.openrouter.apiKey") is False
    assert data == {"model": "m"}


def test_json_unset_absent_top_level():
    assert _json_unset({}, "model") is False


def test_parse_config_value_json_literals():
    assert _parse_config_value("true") is True
    assert _parse_config_value("false") is False
    assert _parse_config_value("42") == 42


def test_parse_config_value_falls_back_to_string():
    # Provider keys and model ids aren't valid JSON; keep them as strings.
    assert _parse_config_value("sk-or-abc123") == "sk-or-abc123"
    assert _parse_config_value("anthropic/claude-sonnet-4-6") == (
        "anthropic/claude-sonnet-4-6"
    )


# ---------------------------------------------------------------------------
# _resolve_profile
# ---------------------------------------------------------------------------


def test_resolve_profile_none_is_none():
    assert _resolve_profile(None, "claude") is None


def test_resolve_profile_returns_builtin():
    assert _resolve_profile("openrouter", "claude") == profiles.BUILTIN["openrouter"]


def test_resolve_profile_unknown_name_exits():
    with pytest.raises(SystemExit) as e:
        _resolve_profile("nosuch", "claude")
    assert "no profile 'nosuch'" in str(e.value)


def test_resolve_profile_wrong_agent_exits():
    # A profile scoped to another agent is an error, not a silent no-op: the
    # run would otherwise look like it had been applied.
    with pytest.raises(SystemExit) as e:
        _resolve_profile("openrouter", "copilot")
    assert "applies to claude" in str(e.value)


# ---------------------------------------------------------------------------
# _session_env precedence
# ---------------------------------------------------------------------------


class _EnvFakeContainer:
    def mount_wayland(self, user):
        return {"WAYLAND_DISPLAY": "wayland-0"}


def _env_for(monkeypatch, dir_env, profile_env):
    """Run _session_env with a stubbed directory env and no proxy."""
    monkeypatch.setattr(state, "get_env", lambda work_dir, agent: dir_env)
    cfg = agents.get("claude")
    container: Any = _EnvFakeContainer()
    return _session_env(container, cfg, {}, Path("/tmp/x"), "claude", profile_env)


def test_session_env_includes_profile_vars(monkeypatch):
    env = _env_for(monkeypatch, {}, {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"})
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"


def test_session_env_directory_beats_profile(monkeypatch):
    # dir > profile: a profile is a reusable default, a variable set for one
    # directory is the more specific statement.
    env = _env_for(monkeypatch, {"FOO": "from-dir"}, {"FOO": "from-profile"})
    assert env["FOO"] == "from-dir"


def test_session_env_aiab_vars_beat_both(monkeypatch):
    # HOME/PATH are managed by aiab and can't be displaced by either source.
    env = _env_for(monkeypatch, {"HOME": "/nope"}, {"HOME": "/also-nope"})
    assert env["HOME"] == CONTAINER_HOME
