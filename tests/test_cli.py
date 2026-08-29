# Tests for the pure helper functions in aiab.cli.
#
# Only the host-side filesystem helpers are covered here; the parts that drive
# LXD (containers, devices) need a live LXD and are exercised manually.

from pathlib import Path
from typing import Any

import pytest

import click

from aiab import CONTAINER_HOME
from aiab import agents, profiles, state

from aiab.cli import (
    _agent_command,
    _drop_stale_home_overlays,
    _guard_git_repo,
    _json_set,
    _json_unset,
    _parse_config_value,
    _private_home_mounts,
    _reseed_file,
    _reseed_tree,
    _resolve_profile,
    _session_env,
    _shared_home_mounts,
    _tmux_group,
    _tmux_group_member,
    _tmux_joined_nothing,
    _tmux_session_name,
)


class FakeContainer:
    """Records add_config_overlay calls instead of touching LXD."""

    def __init__(self, devices=None):
        self.overlays = []
        # container path -> device name, as Container.config_overlays reports.
        self._devices = dict(devices or {})
        self.removed = []

    def add_config_overlay(
        self, host_path, container_path, container_user=0, readonly=False, force=False
    ):
        self.overlays.append((host_path, container_path, readonly))

    def config_overlays(self):
        return {name: path for path, name in self._devices.items()}

    def remove_device(self, name):
        self.removed.append(name)
        return True


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
    with pytest.raises(click.ClickException) as e:
        _resolve_profile("nosuch", "claude")
    assert "no profile 'nosuch'" in str(e.value)


def test_resolve_profile_wrong_agent_exits():
    # A profile scoped to another agent is an error, not a silent no-op: the
    # run would otherwise look like it had been applied.
    with pytest.raises(click.ClickException) as e:
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


# ---------------------------------------------------------------------------
# _agent_command
# ---------------------------------------------------------------------------


def test_agent_command_runs_the_agent_with_args():
    cfg = agents.get("claude")
    assert _agent_command(cfg, ("-c", "echo hi"), shell=False) == [
        cfg.command,
        *cfg.extra_args,
        "-c",
        "echo hi",
    ]


def test_agent_command_shell_with_no_args():
    cfg = agents.get("claude")
    assert _agent_command(cfg, (), shell=True) == ["bash", "-l"]


def test_agent_command_shell_rejects_agent_args():
    # --shell can't pass agent_args through to bash, so it errors instead of
    # silently dropping them.
    cfg = agents.get("claude")
    with pytest.raises(click.UsageError):
        _agent_command(cfg, ("-c", "echo hi"), shell=True)


# ---------------------------------------------------------------------------
# _agent_containers
# ---------------------------------------------------------------------------


def test_agent_containers_yields_isolated_profile_prefixes(monkeypatch, tmp_path):
    # mount/unmount must reach isolated-profile session containers (e.g.
    # claude-openrouter-<slug>), not just the plain agent ones — otherwise
    # unmount can never remove a device it left on a profile container.
    # Mirrors test_profiles.py's session_prefixes coverage, one level up.
    from aiab import lxd
    from aiab.cli import _agent_containers

    class _FakeContainer:
        def __init__(self, name):
            self.name = name

        def exists(self):
            return True

    requested_prefixes = []

    def fake_container_for_dir(self, path, prefix):
        requested_prefixes.append(prefix)
        return _FakeContainer(f"{prefix}-slug")

    monkeypatch.setattr(lxd.Lxd, "container_for_dir", fake_container_for_dir)

    conn = lxd.Lxd("aiab")
    result = list(_agent_containers(conn, tmp_path))

    prefixes = [prefix for prefix, _ in result]
    assert "claude" in prefixes
    assert "claude-openrouter" in prefixes  # the built-in isolated profile
    assert prefixes == requested_prefixes


def test_agent_containers_skips_containers_that_dont_exist(monkeypatch, tmp_path):
    from aiab import lxd
    from aiab.cli import _agent_containers

    class _MissingContainer:
        name = "irrelevant"

        def exists(self):
            return False

    monkeypatch.setattr(
        lxd.Lxd, "container_for_dir", lambda self, path, prefix: _MissingContainer()
    )

    conn = lxd.Lxd("aiab")
    assert list(_agent_containers(conn, tmp_path)) == []


# ---------------------------------------------------------------------------
# tmux session naming
# ---------------------------------------------------------------------------


def test_tmux_group_is_named_for_the_container():
    # The container is the thing two runs share, so it keys the group; a
    # different agent or isolated profile is a different container, and so a
    # different group.
    assert _tmux_group("claude-proj-abc123") == "aiab-claude-proj-abc123"
    assert _tmux_group("claude-openrouter-proj-abc123") != _tmux_group(
        "claude-proj-abc123"
    )


def test_tmux_group_member_finds_the_lone_first_session():
    # The first run's session carries the group name but reports no group of
    # its own until a second session joins it.
    sessions = [("aiab-claude-proj", ""), ("unrelated", "")]
    assert _tmux_group_member("aiab-claude-proj", sessions) == "aiab-claude-proj"


def test_tmux_group_member_finds_a_generated_member():
    # The run that started the group may be gone; any surviving member is a
    # valid join target, and joining via it preserves the group name.
    sessions = [("aiab-claude-proj-3", "aiab-claude-proj")]
    assert _tmux_group_member("aiab-claude-proj", sessions) == "aiab-claude-proj-3"


def test_tmux_group_member_ignores_other_groups():
    sessions = [("aiab-opencode-proj", "aiab-opencode-proj"), ("mine", "")]
    assert _tmux_group_member("aiab-claude-proj", sessions) is None


def test_tmux_group_member_ignores_a_same_named_group_field_only_match():
    # A session whose *name* matches but which belongs to another group must
    # not be offered: joining it would put us in that group.
    sessions = [("aiab-claude-proj", "somebody-elses-group")]
    assert _tmux_group_member("aiab-claude-proj", sessions) is None


def test_tmux_session_name_uses_the_group_when_free():
    assert _tmux_session_name("aiab-claude-proj", set()) == "aiab-claude-proj"


def test_tmux_session_name_skips_taken_names():
    taken = {"aiab-claude-proj", "aiab-claude-proj-2", "aiab-claude-proj-3"}
    assert _tmux_session_name("aiab-claude-proj", taken) == "aiab-claude-proj-4"


def test_tmux_joined_nothing_when_every_window_is_tmuxs_own():
    # `new-session -t` does not fail on a target that has already exited: tmux
    # makes a fresh group with a default shell window, which reports no start
    # command. That is the signal there was nothing to join.
    assert _tmux_joined_nothing([""]) is True


def test_tmux_joined_nothing_false_when_a_real_window_was_shared():
    # An inherited window runs another run's wrapper script. This must stay
    # False even when that run then exits, leaving us holding its live agent
    # window — killing the session then would kill the agent.
    assert _tmux_joined_nothing(["/tmp/aiab-run-xyz.sh"]) is False
    assert _tmux_joined_nothing(["/tmp/aiab-run-xyz.sh", ""]) is False


def test_tmux_joined_nothing_false_when_the_window_list_is_unreadable():
    # Unknown is not "nothing to join": discarding a session we cannot see into
    # could throw away one that is sharing windows. None is a failed query; an
    # empty list means the same, since a session always has one window.
    assert _tmux_joined_nothing(None) is False
    assert _tmux_joined_nothing([]) is False


# ---------------------------------------------------------------------------
# _shared_home_mounts
# ---------------------------------------------------------------------------


def test_shared_home_mounts_map_onto_the_container_home(tmp_path):
    # Each shared path is laid over the directory's own home at the same
    # relative position, so the agent finds its credentials where it expects.
    mounts = _shared_home_mounts(agents.get("claude"), tmp_path)
    by_container = {c: h for h, c in mounts}
    assert f"{CONTAINER_HOME}/.claude.json" in by_container
    assert by_container[f"{CONTAINER_HOME}/.claude/.credentials.json"] == (
        tmp_path / ".claude/.credentials.json"
    )
    # The trailing slash marks a directory but doesn't reach the mount point.
    assert f"{CONTAINER_HOME}/.claude/plugins" in by_container


def test_shared_home_mounts_create_what_the_shared_home_lacks(tmp_path):
    # Both ends of a bind mount must exist, so a fresh shared home is filled in
    # rather than skipped -- skipping would send credentials to the
    # per-directory home, where the next directory wouldn't find them.
    _shared_home_mounts(agents.get("claude"), tmp_path)
    assert (tmp_path / ".claude.json").is_file()
    assert (tmp_path / ".claude/.credentials.json").is_file()
    assert (tmp_path / ".claude/plugins").is_dir()


def test_shared_home_mounts_leave_existing_content_alone(tmp_path):
    creds = tmp_path / ".claude/.credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text('{"token": "secret"}')
    _shared_home_mounts(agents.get("claude"), tmp_path)
    assert creds.read_text() == '{"token": "secret"}'


@pytest.mark.parametrize("name", agents.AGENT_NAMES)
def test_every_agent_shares_something_and_only_under_the_home(name, tmp_path):
    # An agent with no shared paths would ask for a fresh login per directory.
    mounts = _shared_home_mounts(agents.get(name), tmp_path)
    assert mounts
    for host_path, container_path in mounts:
        assert container_path.startswith(f"{CONTAINER_HOME}/")
        assert tmp_path in host_path.parents


# ---------------------------------------------------------------------------
# _private_home_mounts
# ---------------------------------------------------------------------------


def test_copilot_shares_its_directory_not_the_files_in_it(tmp_path):
    # Copilot rewrites config.json by renaming a temp file over it, which is
    # EBUSY when config.json is itself a bind mount (issue #3). So the share
    # is the directory; the file must not be a mount point of its own.
    cfg = agents.get("copilot")
    shared = {c for _h, c in _shared_home_mounts(cfg, tmp_path)}
    assert shared == {f"{CONTAINER_HOME}/.copilot"}
    assert f"{CONTAINER_HOME}/.copilot/config.json" not in shared


def test_private_paths_come_from_the_directorys_own_home(tmp_path):
    # The point of carving these back out: session state that a shared
    # ~/.copilot would otherwise pool across every project directory.
    cfg = agents.get("copilot")
    mounts = _private_home_mounts(cfg, tmp_path)
    by_container = {c: h for h, c in mounts}
    assert by_container[f"{CONTAINER_HOME}/.copilot/session-store.db"] == (
        tmp_path / ".copilot/session-store.db"
    )
    assert (tmp_path / ".copilot/session-state").is_dir()
    assert (tmp_path / ".copilot/session-store.db").is_file()


@pytest.mark.parametrize("name", agents.AGENT_NAMES)
def test_private_paths_sit_inside_a_shared_directory(name, tmp_path):
    # A private path outside every shared directory would be mounting the
    # container's own home back onto itself -- it is already per-directory.
    cfg = agents.get(name)
    shared_dirs = [e.rstrip("/") for e in cfg.shared_paths if e.endswith("/")]
    for entry in cfg.private_paths:
        assert any(entry.startswith(d + "/") for d in shared_dirs), entry


# ---------------------------------------------------------------------------
# _drop_stale_home_overlays
# ---------------------------------------------------------------------------


def test_stale_overlay_inside_a_mounted_directory_is_dropped(tmp_path):
    # The copilot upgrade path: a container from before the move still has a
    # device for the config.json file mount, which would be layered back over
    # the new ~/.copilot directory mount and bring the EBUSY back.
    shared = tmp_path / "shared" / ".copilot"
    shared.mkdir(parents=True)
    container: Any = FakeContainer(
        {f"{CONTAINER_HOME}/.copilot/config.json": "cfg-oldfile"}
    )
    _drop_stale_home_overlays(container, [(shared, f"{CONTAINER_HOME}/.copilot")])
    assert container.removed == ["cfg-oldfile"]


def test_overlays_this_run_wants_are_kept(tmp_path):
    shared = tmp_path / "shared" / ".copilot"
    shared.mkdir(parents=True)
    instructions = tmp_path / "repo" / "copilot-instructions.md"
    instructions.parent.mkdir(parents=True)
    instructions.touch()
    wanted = [
        (shared, f"{CONTAINER_HOME}/.copilot"),
        (instructions, f"{CONTAINER_HOME}/.copilot/copilot-instructions.md"),
    ]
    container: Any = FakeContainer(
        {
            f"{CONTAINER_HOME}/.copilot": "cfg-dir",
            f"{CONTAINER_HOME}/.copilot/copilot-instructions.md": "cfg-md",
        }
    )
    _drop_stale_home_overlays(container, wanted)
    assert container.removed == []


def test_overlays_outside_the_mounted_directories_are_left_alone(tmp_path):
    # The git guard adds its own cfg-* devices under /work later in the run;
    # nothing here may touch them.
    shared = tmp_path / "shared" / ".copilot"
    shared.mkdir(parents=True)
    container: Any = FakeContainer({"/work/repo/.git/config": "cfg-gitguard"})
    _drop_stale_home_overlays(container, [(shared, f"{CONTAINER_HOME}/.copilot")])
    assert container.removed == []
