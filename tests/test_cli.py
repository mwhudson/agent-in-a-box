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
    _guard_git_repo,
    _json_set,
    _json_unset,
    _parse_config_value,
    _reseed_file,
    _reseed_tree,
    _resolve_profile,
    _resolve_shared_tree,
    _session_env,
    _setup_worktree,
    _tmux_group,
    _tmux_group_member,
    _tmux_joined_nothing,
    _tmux_session_name,
    _tmux_window_name,
    _worktree_add_args,
    worktree_path_for,
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
# _resolve_shared_tree
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_tree(monkeypatch, tmp_path):
    """A repo dir, a stubbed lock probe, and a tty by default."""
    from aiab import cli, lifecycle

    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("AIAB_CONCURRENT_DECISION", raising=False)
    monkeypatch.setattr(lifecycle, "session_in_use", lambda name: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    return tmp_path


def test_resolve_shared_tree_passes_through_when_nothing_else_runs(
    monkeypatch, shared_tree
):
    from aiab import lifecycle

    monkeypatch.setattr(lifecycle, "session_in_use", lambda name: False)
    assert _resolve_shared_tree("c", shared_tree, worktree=False) is False


def test_resolve_shared_tree_does_not_probe_when_worktree_already_asked_for(
    monkeypatch, shared_tree
):
    # --worktree already avoids the shared tree, so there is nothing to ask.
    from aiab import lifecycle

    def boom(name):
        raise AssertionError("should not probe when --worktree was given")

    monkeypatch.setattr(lifecycle, "session_in_use", boom)
    assert _resolve_shared_tree("c", shared_tree, worktree=True) is True


def test_resolve_shared_tree_obeys_the_inherited_decision(monkeypatch, shared_tree):
    # The inner process must not ask again; it reads the outer one's answer.
    def boom(*a, **k):
        raise AssertionError("the inner process must not prompt")

    monkeypatch.setattr(click, "prompt", boom)
    monkeypatch.setenv("AIAB_CONCURRENT_DECISION", "worktree")
    assert _resolve_shared_tree("c", shared_tree, worktree=False) is True
    monkeypatch.setenv("AIAB_CONCURRENT_DECISION", "continue")
    assert _resolve_shared_tree("c", shared_tree, worktree=False) is False


def test_resolve_shared_tree_keeps_an_explicit_worktree_across_the_reexec(
    monkeypatch, shared_tree
):
    # 'continue' is what the outer process records when --worktree was passed
    # but no session clashed; it must not cancel the flag.
    monkeypatch.setenv("AIAB_CONCURRENT_DECISION", "continue")
    assert _resolve_shared_tree("c", shared_tree, worktree=True) is True


@pytest.mark.parametrize(
    "choice,expected", [("worktree", True), ("continue", False)]
)
def test_resolve_shared_tree_applies_the_answer(
    monkeypatch, shared_tree, choice, expected
):
    monkeypatch.setattr(click, "prompt", lambda *a, **k: choice)
    assert _resolve_shared_tree("c", shared_tree, worktree=False) is expected


def test_resolve_shared_tree_exit_aborts(monkeypatch, shared_tree):
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "exit")
    with pytest.raises(click.Abort):
        _resolve_shared_tree("c", shared_tree, worktree=False)


def test_resolve_shared_tree_warns_without_asking_when_not_a_tty(
    monkeypatch, shared_tree, capsys
):
    # Non-interactive callers must not block on a prompt they cannot answer.
    from aiab import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        click, "prompt", lambda *a, **k: pytest.fail("must not prompt")
    )
    assert _resolve_shared_tree("c", shared_tree, worktree=False) is False
    assert "already running" in capsys.readouterr().err


def test_resolve_shared_tree_offers_only_two_answers_without_a_repo(
    monkeypatch, tmp_path
):
    # No .git, so `git worktree add` would fail: confirm instead of offering it.
    from aiab import cli, lifecycle

    monkeypatch.delenv("AIAB_CONCURRENT_DECISION", raising=False)
    monkeypatch.setattr(lifecycle, "session_in_use", lambda name: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        click, "prompt", lambda *a, **k: pytest.fail("must not offer a worktree")
    )
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
    assert _resolve_shared_tree("c", tmp_path, worktree=False) is False

    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
    with pytest.raises(click.Abort):
        _resolve_shared_tree("c", tmp_path, worktree=False)


# ---------------------------------------------------------------------------
# worktrees
# ---------------------------------------------------------------------------


def test_worktree_path_is_named_for_the_branch():
    # The branch is the useful label, and '/' in it just nests the path.
    assert (
        worktree_path_for("/work/proj", "feature/x")
        == "/work/proj/.git/aiab-worktrees/feature/x"
    )


def test_worktree_path_without_a_branch_is_unique_per_run():
    # Nothing to name it after, so two runs must still not collide.
    first = worktree_path_for("/work/proj", None)
    second = worktree_path_for("/work/proj", None)
    assert first != second
    assert first.startswith("/work/proj/.git/aiab-worktrees/")


def test_worktree_add_args_detached_without_a_branch():
    assert _worktree_add_args("/w/p", None, branch_exists=False) == [
        "worktree",
        "add",
        "--detach",
        "/w/p",
    ]


def test_worktree_add_args_creates_a_new_branch_with_dash_b():
    assert _worktree_add_args("/w/p", "topic", branch_exists=False) == [
        "worktree",
        "add",
        "-b",
        "topic",
        "/w/p",
    ]


def test_worktree_add_args_checks_out_an_existing_branch_without_dash_b():
    # -b on a branch that exists fails outright rather than reusing it.
    assert _worktree_add_args("/w/p", "topic", branch_exists=True) == [
        "worktree",
        "add",
        "/w/p",
        "topic",
    ]


def test_tmux_window_name_includes_the_branch():
    # ':' would collide with tmux's session:window target syntax.
    assert _tmux_window_name("claude", "feature/x") == "claude@feature/x"
    assert _tmux_window_name("claude", None) == "claude"


class LocalGitContainer:
    """Runs the container's git commands on the host against a real repo.

    _setup_worktree's logic is all in *which* git commands it chooses and how it
    reads their results, so pointing them at a real repo tests the thing that
    matters. Strips the `runuser -u <login> --` prefix aiab uses to drop out of
    root inside the container.
    """

    def exec(self, cmd, *, cwd=None, user=None, check=True, **kwargs):
        import subprocess as sp

        if cmd[:2] == ["runuser", "-u"]:
            cmd = cmd[4:]
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return sp.run(cmd, cwd=cwd, check=check, **kwargs)


def _local_git() -> Any:
    """LocalGitContainer, typed loose like the other fakes here."""
    return LocalGitContainer()


@pytest.fixture
def git_repo(tmp_path):
    import subprocess as sp

    repo = tmp_path / "proj"
    repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "HOME": str(tmp_path)}
    sp.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True, env=env)
    sp.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f").write_text("x\n")
    sp.run(["git", "add", "f"], cwd=repo, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _head_of(path):
    import subprocess as sp

    return sp.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_setup_worktree_creates_a_branch_and_checks_it_out(git_repo):
    path = _setup_worktree(_local_git(), str(git_repo), 1000, "topic")
    assert path == f"{git_repo}/.git/aiab-worktrees/topic"
    assert _head_of(path) == "topic"


def test_setup_worktree_detaches_without_a_branch(git_repo):
    path = _setup_worktree(_local_git(), str(git_repo), 1000)
    assert _head_of(path) == "HEAD"  # detached


def test_setup_worktree_reenters_an_existing_branch(git_repo):
    # -b would fail here; the run should resume the branch instead. Uses a
    # second path so this is the "branch exists, directory doesn't" case.
    import subprocess as sp

    sp.run(["git", "branch", "topic"], cwd=git_repo, check=True)
    path = _setup_worktree(_local_git(), str(git_repo), 1000, "topic")
    assert _head_of(path) == "topic"


def test_setup_worktree_reuses_its_own_leftover_directory(git_repo):
    # What --worktree-keep (or a crash) leaves behind: same branch, same path.
    first = _setup_worktree(_local_git(), str(git_repo), 1000, "topic")
    (Path(first) / "scratch").write_text("agent's work\n")
    second = _setup_worktree(_local_git(), str(git_repo), 1000, "topic")
    assert second == first
    assert (Path(second) / "scratch").read_text() == "agent's work\n"


def test_setup_worktree_refuses_a_directory_holding_another_branch(git_repo):
    # Never silently clobber something already in the way.
    import subprocess as sp

    other = git_repo / ".git" / "aiab-worktrees" / "topic"
    sp.run(
        ["git", "worktree", "add", "-b", "different", str(other)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    with pytest.raises(click.ClickException, match="different"):
        _setup_worktree(_local_git(), str(git_repo), 1000, "topic")


def test_setup_worktree_surfaces_gits_error_for_a_branch_in_use(git_repo):
    # Another run already has this branch checked out; git's message says so.
    import subprocess as sp

    sp.run(
        ["git", "worktree", "add", "-b", "topic", str(git_repo / "elsewhere")],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    with pytest.raises(click.ClickException, match="already used by worktree"):
        _setup_worktree(_local_git(), str(git_repo), 1000, "topic")


def test_setup_worktree_surfaces_gits_error_for_a_bad_branch_name(git_repo):
    with pytest.raises(click.ClickException):
        _setup_worktree(_local_git(), str(git_repo), 1000, "bad name")


def test_setup_worktree_rejects_a_non_repo(tmp_path):
    with pytest.raises(click.ClickException, match="requires a git repository"):
        _setup_worktree(_local_git(), str(tmp_path), 1000, "topic")
