# Tests for aiab.state — the per-directory mount, network-policy, base-release,
# resource-limits, and state-dir records.
#
# All tests redirect _PATH, _NET_PATH, _BASE_PATH, _LIMITS_PATH and
# _DIRSTATE_DIR to a tmp dir so no real state is read or written.

from concurrent.futures import ProcessPoolExecutor
import time
from pathlib import Path

import pytest

import aiab.state as state


def _set_mount_slow_load(root, project, source):
    import aiab.state as child_state

    child_state._PATH = root / "mounts.json"
    original_load_file = child_state._load_file

    def slow_load_file(path):
        data = original_load_file(path)
        if path == child_state._PATH:
            time.sleep(0.05)
        return data

    child_state._load_file = slow_load_file
    child_state.set_mount(project, source, readonly=True)


def _add_network_allow_slow_load(root, project, domain):
    import aiab.state as child_state

    child_state._NET_PATH = root / "network.json"
    original_load_file = child_state._load_file

    def slow_load_file(path):
        data = original_load_file(path)
        if path == child_state._NET_PATH:
            time.sleep(0.05)
        return data

    child_state._load_file = slow_load_file
    child_state.add_network_allow(project, domain, expires=None)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect all state I/O to a temporary directory."""
    monkeypatch.setattr(state, "_PATH", tmp_path / "mounts.json")
    monkeypatch.setattr(state, "_NET_PATH", tmp_path / "network.json")
    monkeypatch.setattr(state, "_BASE_PATH", tmp_path / "base.json")
    monkeypatch.setattr(state, "_LIMITS_PATH", tmp_path / "limits.json")
    monkeypatch.setattr(state, "_ENV_PATH", tmp_path / "env.json")
    monkeypatch.setattr(state, "_DIRSTATE_DIR", tmp_path / "dirstate")


# ---------------------------------------------------------------------------
# Mount records
# ---------------------------------------------------------------------------


def test_get_mounts_empty(tmp_path):
    assert state.get_mounts(tmp_path) == []


def test_set_and_get_mount(tmp_path):
    src = tmp_path / "src"
    state.set_mount(tmp_path, src, readonly=True)
    mounts = state.get_mounts(tmp_path)
    assert len(mounts) == 1
    assert mounts[0]["source"] == str(src)
    assert mounts[0]["readonly"] is True


def test_set_mount_updates_mode(tmp_path):
    src = tmp_path / "src"
    state.set_mount(tmp_path, src, readonly=True)
    state.set_mount(tmp_path, src, readonly=False)
    mounts = state.get_mounts(tmp_path)
    assert len(mounts) == 1
    assert mounts[0]["readonly"] is False


def test_set_mount_multiple_sources(tmp_path):
    src_a = tmp_path / "a"
    src_b = tmp_path / "b"
    state.set_mount(tmp_path, src_a, readonly=True)
    state.set_mount(tmp_path, src_b, readonly=False)
    sources = {m["source"] for m in state.get_mounts(tmp_path)}
    assert sources == {str(src_a), str(src_b)}


def test_remove_mount_present(tmp_path):
    src = tmp_path / "src"
    state.set_mount(tmp_path, src, readonly=True)
    removed = state.remove_mount(tmp_path, src)
    assert removed is True
    assert state.get_mounts(tmp_path) == []


def test_remove_mount_absent(tmp_path):
    removed = state.remove_mount(tmp_path, tmp_path / "nonexistent")
    assert removed is False


def test_remove_mount_clears_key(tmp_path):
    # When the last mount for a dir is removed the key itself is gone.
    src = tmp_path / "src"
    state.set_mount(tmp_path, src, readonly=True)
    state.remove_mount(tmp_path, src)
    data = state._load()
    assert str(tmp_path) not in data


def test_mounts_are_keyed_per_directory(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    src = tmp_path / "src"
    state.set_mount(dir_a, src, readonly=True)
    assert state.get_mounts(dir_b) == []


def test_concurrent_set_mounts_keep_all_sources(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    sources = [tmp_path / f"src-{i}" for i in range(4)]

    with ProcessPoolExecutor(max_workers=len(sources)) as pool:
        list(
            pool.map(
                _set_mount_slow_load,
                [tmp_path] * len(sources),
                [project] * len(sources),
                sources,
            )
        )

    assert {m["source"] for m in state.get_mounts(project)} == {
        str(s) for s in sources
    }


# ---------------------------------------------------------------------------
# Network policy
# ---------------------------------------------------------------------------


def test_get_network_default(tmp_path):
    policy = state.get_network(tmp_path)
    assert policy["mode"] == state.DEFAULT_MODE
    assert policy["allow"] == []


def test_set_network_mode_open_recorded(tmp_path):
    # Open is not the default, so an explicit `net open` must persist.
    state.set_network_mode(tmp_path, state.MODE_OPEN)
    assert state.get_network(tmp_path)["mode"] == state.MODE_OPEN


def test_set_network_mode_back_to_default_removes_key(tmp_path):
    state.set_network_mode(tmp_path, state.MODE_OPEN)
    state.set_network_mode(tmp_path, state.DEFAULT_MODE)
    # The default mode with no allows → key is pruned from file.
    data = state._load_file(state._NET_PATH)
    assert str(tmp_path) not in data


def test_add_network_allow(tmp_path):
    state.set_network_mode(tmp_path, state.MODE_RESTRICTED)
    state.add_network_allow(tmp_path, "example.com", expires=None)
    policy = state.get_network(tmp_path)
    assert any(a["domain"] == "example.com" for a in policy["allow"])


def test_add_network_allow_normalises_domain(tmp_path):
    state.set_network_mode(tmp_path, state.MODE_RESTRICTED)
    state.add_network_allow(tmp_path, "*.Example.COM.", expires=None)
    policy = state.get_network(tmp_path)
    assert policy["allow"][0]["domain"] == "example.com"


def test_add_network_allow_replaces_expiry(tmp_path):
    state.set_network_mode(tmp_path, state.MODE_RESTRICTED)
    state.add_network_allow(tmp_path, "example.com", expires=9999999999.0)
    state.add_network_allow(tmp_path, "example.com", expires=None)
    policy = state.get_network(tmp_path)
    allows = [a for a in policy["allow"] if a["domain"] == "example.com"]
    assert len(allows) == 1
    assert allows[0]["expires"] is None


def test_remove_network_allow(tmp_path):
    state.set_network_mode(tmp_path, state.MODE_RESTRICTED)
    state.add_network_allow(tmp_path, "example.com", expires=None)
    removed = state.remove_network_allow(tmp_path, "example.com")
    assert removed is True
    policy = state.get_network(tmp_path)
    assert not any(a["domain"] == "example.com" for a in policy["allow"])


def test_remove_network_allow_absent(tmp_path):
    removed = state.remove_network_allow(tmp_path, "example.com")
    assert removed is False


def test_global_allow_recorded_separately(tmp_path):
    state.add_network_allow(None, "example.com", expires=None, global_=True)
    # The global rule is on the shared list, not on any directory's policy.
    assert any(
        a["domain"] == "example.com" for a in state.get_global_network()["allow"]
    )
    assert state.get_network(tmp_path)["allow"] == []


def test_global_deny_recorded_separately(tmp_path):
    state.add_network_deny(None, "tracker.example", global_=True)
    assert state.get_global_network()["deny"] == ["tracker.example"]
    assert state.get_network(tmp_path)["deny"] == []


def test_global_allow_deny_disjoint(tmp_path):
    state.add_network_allow(None, "example.com", expires=None, global_=True)
    state.add_network_deny(None, "example.com", global_=True)
    glob = state.get_global_network()
    assert glob["deny"] == ["example.com"]
    assert not any(a["domain"] == "example.com" for a in glob["allow"])


def test_remove_global_allow(tmp_path):
    state.add_network_allow(None, "example.com", expires=None, global_=True)
    assert state.remove_network_allow(None, "example.com", global_=True) is True
    assert state.get_global_network()["allow"] == []


def test_global_default_empty(tmp_path):
    glob = state.get_global_network()
    assert glob["allow"] == []
    assert glob["deny"] == []


# -- per-agent axis --


def test_agent_allow_isolated_to_agent(tmp_path):
    state.add_network_allow(tmp_path, "mcp.example", expires=None, agent="opencode")
    # The all-agents view and other agents don't see the rule...
    assert state.get_network(tmp_path)["allow"] == []
    assert state.network_for_agent(tmp_path, "claude")["allow"] == []
    # ...but the named agent's flattened view does.
    flat = state.network_for_agent(tmp_path, "opencode")
    assert any(a["domain"] == "mcp.example" for a in flat["allow"])


def test_agent_flatten_merges_all_agents_and_agent(tmp_path):
    state.add_network_allow(tmp_path, "shared.example", expires=None)
    state.add_network_allow(tmp_path, "mcp.example", expires=None, agent="opencode")
    flat = state.network_for_agent(tmp_path, "opencode")
    domains = {a["domain"] for a in flat["allow"]}
    assert domains == {"shared.example", "mcp.example"}


def test_agent_deny_isolated(tmp_path):
    state.add_network_deny(tmp_path, "ads.example", agent="opencode")
    assert state.get_network(tmp_path)["deny"] == []
    assert state.network_for_agent(tmp_path, "opencode")["deny"] == ["ads.example"]
    assert state.network_for_agent(tmp_path, "claude")["deny"] == []


def test_global_agent_rule(tmp_path):
    state.add_network_allow(
        None, "mcp.example", expires=None, global_=True, agent="opencode"
    )
    assert state.global_for_agent("opencode")["allow"][0]["domain"] == "mcp.example"
    assert state.global_for_agent("claude")["allow"] == []


def test_agent_allow_deny_disjoint(tmp_path):
    state.add_network_allow(tmp_path, "x.example", expires=None, agent="opencode")
    state.add_network_deny(tmp_path, "x.example", agent="opencode")
    flat = state.network_for_agent(tmp_path, "opencode")
    assert flat["deny"] == ["x.example"]
    assert flat["allow"] == []


def test_remove_agent_allow_empties_record(tmp_path):
    state.add_network_allow(tmp_path, "x.example", expires=None, agent="opencode")
    assert state.remove_network_allow(tmp_path, "x.example", agent="opencode") is True
    # The now-empty agent overlay is compacted away, dropping the whole record.
    assert str(tmp_path) not in state._load_file(state._NET_PATH)


def test_agent_rule_keeps_all_agents_rule_on_compact(tmp_path):
    state.add_network_allow(tmp_path, "keep.example", expires=None)
    state.add_network_allow(tmp_path, "tmp.example", expires=None, agent="opencode")
    state.remove_network_allow(tmp_path, "tmp.example", agent="opencode")
    policy = state.get_network(tmp_path)
    assert {a["domain"] for a in policy["allow"]} == {"keep.example"}
    assert "agents" not in policy or policy["agents"] == {}


def test_old_record_without_agents_key_still_loads(tmp_path):
    # A policy written before the agent axis existed has no "agents" key.
    state._save_file(
        state._NET_PATH,
        {str(tmp_path.resolve()): {"mode": "restricted", "allow": [], "deny": ["x"]}},
    )
    policy = state.get_network(tmp_path)
    assert policy["deny"] == ["x"]
    assert state.network_for_agent(tmp_path, "opencode")["deny"] == ["x"]


def test_expired_allow_filtered_from_get(tmp_path):
    state.set_network_mode(tmp_path, state.MODE_RESTRICTED)
    past = time.time() - 1
    state.add_network_allow(tmp_path, "expired.com", expires=past)
    policy = state.get_network(tmp_path)
    assert not any(a["domain"] == "expired.com" for a in policy["allow"])


def test_unexpired_allow_visible(tmp_path):
    state.set_network_mode(tmp_path, state.MODE_RESTRICTED)
    future = time.time() + 3600
    state.add_network_allow(tmp_path, "future.com", expires=future)
    policy = state.get_network(tmp_path)
    assert any(a["domain"] == "future.com" for a in policy["allow"])


def test_concurrent_network_allows_keep_all_domains(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    domains = [f"example-{i}.com" for i in range(4)]

    with ProcessPoolExecutor(max_workers=len(domains)) as pool:
        list(
            pool.map(
                _add_network_allow_slow_load,
                [tmp_path] * len(domains),
                [project] * len(domains),
                domains,
            )
        )

    assert {a["domain"] for a in state.get_network(project)["allow"]} == set(
        domains
    )


# ---------------------------------------------------------------------------
# Base-release records
# ---------------------------------------------------------------------------


def test_get_base_default_when_unset(tmp_path):
    from aiab import release

    assert state.get_base(tmp_path) == release.DEFAULT_BASE


def test_set_and_get_base(tmp_path):
    state.set_base(tmp_path, "22.04")
    assert state.get_base(tmp_path) == "22.04"


def test_set_base_to_default_drops_record(tmp_path):
    from aiab import release

    state.set_base(tmp_path, "22.04")
    state.set_base(tmp_path, release.DEFAULT_BASE)
    # Back to default, so the file should hold no entry for this dir.
    assert state.get_base(tmp_path) == release.DEFAULT_BASE
    assert (
        not (tmp_path / "base.json").exists()
        or "22.04" not in (tmp_path / "base.json").read_text()
    )


def test_prune_stale_removes_deleted_base_dirs(tmp_path):
    from aiab import release

    gone = tmp_path / "gone"
    state.set_base(gone, "22.04")

    _, _, pruned_base, _, _, _ = state.prune_stale()

    assert str(gone) in pruned_base
    assert state.get_base(gone) == release.DEFAULT_BASE


# ---------------------------------------------------------------------------
# prune_stale
# ---------------------------------------------------------------------------


def test_prune_stale_removes_deleted_mount_dirs(tmp_path):
    gone = tmp_path / "gone"  # never created — does not exist
    here = tmp_path / "here"
    here.mkdir()
    state.set_mount(gone, tmp_path / "src", readonly=True)
    state.set_mount(here, tmp_path / "src", readonly=True)

    pruned_mounts, _, _, _, _, _ = state.prune_stale()

    assert str(gone) in pruned_mounts
    assert str(here) not in pruned_mounts
    assert state.get_mounts(gone) == []
    assert len(state.get_mounts(here)) == 1


def test_prune_stale_removes_deleted_network_dirs(tmp_path):
    gone = tmp_path / "gone"
    # An explicit open record is the non-default policy that persists.
    state.set_network_mode(gone, state.MODE_OPEN)

    _, pruned_net, _, _, _, _ = state.prune_stale()

    assert str(gone) in pruned_net


def test_prune_stale_keeps_global_network(tmp_path):
    # The global policy belongs to no directory, so prune must never drop it.
    state.add_network_allow(None, "example.com", expires=None, global_=True)

    _, pruned_net, _, _, _, _ = state.prune_stale()

    assert state.GLOBAL_KEY not in pruned_net
    assert any(
        a["domain"] == "example.com" for a in state.get_global_network()["allow"]
    )


def test_prune_stale_no_op_when_clean(tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    state.set_mount(here, tmp_path / "src", readonly=True)

    assert state.prune_stale() == ([], [], [], [], [], [])


def test_prune_stale_empty_files(tmp_path):
    # Should not raise when there's nothing in any file.
    assert state.prune_stale() == ([], [], [], [], [], [])


# ---------------------------------------------------------------------------
# dir_state_dir
# ---------------------------------------------------------------------------


def test_dir_state_dir_creates_dir_and_source(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    d = state.dir_state_dir(project)
    assert d.is_dir()
    assert (d / ".source").read_text().strip() == str(project)


def test_dir_state_dir_is_stable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert state.dir_state_dir(project) == state.dir_state_dir(project)


def test_dir_state_dir_distinguishes_same_basename(tmp_path):
    a = tmp_path / "a" / "project"
    b = tmp_path / "b" / "project"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert state.dir_state_dir(a) != state.dir_state_dir(b)


def test_dir_state_dir_preserves_contents(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (state.dir_state_dir(project) / "setup.sh").write_text("echo hi\n")
    assert (state.dir_state_dir(project) / "setup.sh").read_text() == "echo hi\n"


def test_prune_stale_removes_state_dir_for_deleted_dir(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    here = tmp_path / "here"
    here.mkdir()
    gone_state = state.dir_state_dir(gone)
    here_state = state.dir_state_dir(here)
    gone.rmdir()

    _, _, _, pruned_state, _, _ = state.prune_stale()

    assert pruned_state == [str(gone)]
    assert not gone_state.exists()
    assert here_state.is_dir()


def test_prune_stale_skips_state_dir_without_source(tmp_path):
    stray = state._DIRSTATE_DIR / "stray"
    stray.mkdir(parents=True)

    _, _, _, pruned_state, _, _ = state.prune_stale()

    assert pruned_state == []
    assert stray.is_dir()


# ---------------------------------------------------------------------------
# git_guard_dir
# ---------------------------------------------------------------------------


def test_git_guard_dir_lives_under_state_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    guard = state.git_guard_dir(project)
    assert guard.is_dir()
    assert guard.parent == state.dir_state_dir(project)


def test_git_guard_dir_is_stable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert state.git_guard_dir(project) == state.git_guard_dir(project)


def test_git_guard_dir_pruned_with_state_dir(tmp_path):
    # It lives inside the dir_state_dir, so prune_stale reclaims it with the
    # rest of a deleted directory's state.
    gone = tmp_path / "gone"
    gone.mkdir()
    guard = state.git_guard_dir(gone)
    (guard / "config").write_text("[core]\n")
    gone.rmdir()

    state.prune_stale()

    assert not guard.exists()


def test_git_guard_dir_per_mount_is_distinct(tmp_path):
    # A mount's guard dir nests under the directory's own guard dir, and two
    # different mount sources get distinct subdirs so they don't collide.
    project = tmp_path / "project"
    project.mkdir()
    own = state.git_guard_dir(project)
    a = state.git_guard_dir(project, tmp_path / "mount-a")
    b = state.git_guard_dir(project, tmp_path / "mount-b")

    assert a.parent == own and b.parent == own
    assert a != b
    assert a.is_dir() and b.is_dir()


def test_git_guard_dir_per_mount_is_stable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    src = tmp_path / "mount"
    assert state.git_guard_dir(project, src) == state.git_guard_dir(project, src)


def test_git_guard_dir_per_mount_pruned_with_state_dir(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    guard = state.git_guard_dir(gone, tmp_path / "mount")
    (guard / "config").write_text("[core]\n")
    gone.rmdir()

    state.prune_stale()

    assert not guard.exists()


# ---------------------------------------------------------------------------
# Network denylist
# ---------------------------------------------------------------------------


def test_get_network_default_deny_empty(tmp_path):
    assert state.get_network(tmp_path)["deny"] == []


def test_get_network_backfills_deny_for_old_records(tmp_path):
    # Policy records written before the denylist existed have no "deny" key.
    state._save_file(
        state._NET_PATH,
        {str(tmp_path): {"mode": state.MODE_OPEN, "allow": []}},
    )
    assert state.get_network(tmp_path)["deny"] == []


def test_add_network_deny(tmp_path):
    state.add_network_deny(tmp_path, "example.com")
    assert state.get_network(tmp_path)["deny"] == ["example.com"]


def test_add_network_deny_normalises_and_dedups(tmp_path):
    state.add_network_deny(tmp_path, "*.Example.COM.")
    state.add_network_deny(tmp_path, "example.com")
    assert state.get_network(tmp_path)["deny"] == ["example.com"]


def test_add_network_deny_drops_allow(tmp_path):
    state.add_network_allow(tmp_path, "example.com", expires=None)
    state.add_network_deny(tmp_path, "example.com")
    policy = state.get_network(tmp_path)
    assert policy["allow"] == []
    assert policy["deny"] == ["example.com"]


def test_add_network_allow_drops_deny(tmp_path):
    state.add_network_deny(tmp_path, "example.com")
    state.add_network_allow(tmp_path, "example.com", expires=None)
    policy = state.get_network(tmp_path)
    assert policy["deny"] == []
    assert [a["domain"] for a in policy["allow"]] == ["example.com"]


def test_remove_network_deny(tmp_path):
    state.add_network_deny(tmp_path, "example.com")
    assert state.remove_network_deny(tmp_path, "example.com") is True
    assert state.get_network(tmp_path)["deny"] == []


def test_remove_network_deny_absent(tmp_path):
    assert state.remove_network_deny(tmp_path, "example.com") is False


def test_deny_alone_keeps_record(tmp_path):
    # A default-mode policy with only denies must still be persisted.
    state.add_network_deny(tmp_path, "example.com")
    data = state._load_file(state._NET_PATH)
    assert str(tmp_path) in data
    state.remove_network_deny(tmp_path, "example.com")
    data = state._load_file(state._NET_PATH)
    assert str(tmp_path) not in data


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


def test_get_limits_returns_defaults_when_unset(tmp_path):
    assert state.get_limits(tmp_path) == state.DEFAULT_LIMITS


def test_set_and_get_limits(tmp_path):
    new = {"cpu": 8, "memory": "16GiB"}
    state.set_limits(tmp_path, new)
    assert state.get_limits(tmp_path) == new


def test_set_limits_partial_update(tmp_path):
    current = dict(state.DEFAULT_LIMITS)
    current["cpu"] = 8
    state.set_limits(tmp_path, current)
    got = state.get_limits(tmp_path)
    assert got["cpu"] == 8
    assert got["memory"] == state.DEFAULT_LIMITS["memory"]


def test_set_limits_to_default_drops_record(tmp_path):
    state.set_limits(tmp_path, {"cpu": 8, "memory": "16GiB"})
    state.set_limits(tmp_path, state.DEFAULT_LIMITS)
    data = state._load_file(state._LIMITS_PATH)
    assert str(tmp_path) not in data


def test_limits_keyed_per_directory(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    state.set_limits(dir_a, {"cpu": 2, "memory": "4GiB"})
    assert state.get_limits(dir_b) == state.DEFAULT_LIMITS


def test_prune_stale_removes_deleted_limits_dirs(tmp_path):
    gone = tmp_path / "gone"
    state.set_limits(gone, {"cpu": 8, "memory": "16GiB"})

    _, _, _, _, pruned_limits, _ = state.prune_stale()

    assert str(gone) in pruned_limits
    assert state.get_limits(gone) == state.DEFAULT_LIMITS


# ---------------------------------------------------------------------------
# Injected environment variables
# ---------------------------------------------------------------------------


def test_get_env_empty(tmp_path):
    assert state.get_env(tmp_path, "opencode") == {}


def test_set_and_get_env_all_agents(tmp_path):
    state.set_env(tmp_path, state.ENV_ALL_AGENTS, "FOO", "bar")
    # The "*" bucket reaches every agent.
    assert state.get_env(tmp_path, "opencode") == {"FOO": "bar"}
    assert state.get_env(tmp_path, "claude") == {"FOO": "bar"}


def test_set_and_get_env_per_agent(tmp_path):
    state.set_env(tmp_path, "opencode", "OPENCODE_CONFIG", "/x")
    assert state.get_env(tmp_path, "opencode") == {"OPENCODE_CONFIG": "/x"}
    # A different agent doesn't see another agent's bucket.
    assert state.get_env(tmp_path, "claude") == {}


def test_get_env_agent_overrides_all_agents(tmp_path):
    state.set_env(tmp_path, state.ENV_ALL_AGENTS, "K", "shared")
    state.set_env(tmp_path, "opencode", "K", "specific")
    assert state.get_env(tmp_path, "opencode") == {"K": "specific"}
    assert state.get_env(tmp_path, "claude") == {"K": "shared"}


def test_get_env_merges_buckets(tmp_path):
    state.set_env(tmp_path, state.ENV_ALL_AGENTS, "A", "1")
    state.set_env(tmp_path, "opencode", "B", "2")
    assert state.get_env(tmp_path, "opencode") == {"A": "1", "B": "2"}


def test_set_env_updates_value(tmp_path):
    state.set_env(tmp_path, "opencode", "K", "old")
    state.set_env(tmp_path, "opencode", "K", "new")
    assert state.get_env(tmp_path, "opencode") == {"K": "new"}


def test_unset_env_present(tmp_path):
    state.set_env(tmp_path, "opencode", "K", "v")
    assert state.unset_env(tmp_path, "opencode", "K") is True
    assert state.get_env(tmp_path, "opencode") == {}


def test_unset_env_absent(tmp_path):
    assert state.unset_env(tmp_path, "opencode", "K") is False


def test_unset_env_clears_empty_buckets(tmp_path):
    state.set_env(tmp_path, "opencode", "K", "v")
    state.unset_env(tmp_path, "opencode", "K")
    # Both the agent bucket and the directory entry are pruned.
    assert str(tmp_path) not in state._load_file(state._ENV_PATH)


def test_unset_env_keeps_other_buckets(tmp_path):
    state.set_env(tmp_path, state.ENV_ALL_AGENTS, "A", "1")
    state.set_env(tmp_path, "opencode", "B", "2")
    state.unset_env(tmp_path, "opencode", "B")
    assert state.list_env(tmp_path) == {state.ENV_ALL_AGENTS: {"A": "1"}}


def test_list_env(tmp_path):
    state.set_env(tmp_path, state.ENV_ALL_AGENTS, "A", "1")
    state.set_env(tmp_path, "opencode", "B", "2")
    assert state.list_env(tmp_path) == {
        state.ENV_ALL_AGENTS: {"A": "1"},
        "opencode": {"B": "2"},
    }


def test_env_keyed_per_directory(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    state.set_env(dir_a, state.ENV_ALL_AGENTS, "K", "v")
    assert state.get_env(dir_b, "opencode") == {}


def test_prune_stale_removes_deleted_env_dirs(tmp_path):
    gone = tmp_path / "gone"
    state.set_env(gone, state.ENV_ALL_AGENTS, "K", "v")

    *_, pruned_env = state.prune_stale()

    assert str(gone) in pruned_env
    assert state.get_env(gone, "opencode") == {}
