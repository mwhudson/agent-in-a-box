# Tests for aiab.state — the per-directory mount, network-policy and state-dir
# records.
#
# All tests redirect _PATH, _NET_PATH and _DIRSTATE_DIR to a tmp dir so no
# real state is read or written.

import time
from pathlib import Path

import pytest

import aiab.state as state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect all state I/O to a temporary directory."""
    monkeypatch.setattr(state, "_PATH", tmp_path / "mounts.json")
    monkeypatch.setattr(state, "_NET_PATH", tmp_path / "network.json")
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


# ---------------------------------------------------------------------------
# prune_stale
# ---------------------------------------------------------------------------


def test_prune_stale_removes_deleted_mount_dirs(tmp_path):
    gone = tmp_path / "gone"  # never created — does not exist
    here = tmp_path / "here"
    here.mkdir()
    state.set_mount(gone, tmp_path / "src", readonly=True)
    state.set_mount(here, tmp_path / "src", readonly=True)

    pruned_mounts, _, _ = state.prune_stale()

    assert str(gone) in pruned_mounts
    assert str(here) not in pruned_mounts
    assert state.get_mounts(gone) == []
    assert len(state.get_mounts(here)) == 1


def test_prune_stale_removes_deleted_network_dirs(tmp_path):
    gone = tmp_path / "gone"
    # An explicit open record is the non-default policy that persists.
    state.set_network_mode(gone, state.MODE_OPEN)

    _, pruned_net, _ = state.prune_stale()

    assert str(gone) in pruned_net


def test_prune_stale_no_op_when_clean(tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    state.set_mount(here, tmp_path / "src", readonly=True)

    assert state.prune_stale() == ([], [], [])


def test_prune_stale_empty_files(tmp_path):
    # Should not raise when there's nothing in either file.
    assert state.prune_stale() == ([], [], [])


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

    _, _, pruned_state = state.prune_stale()

    assert pruned_state == [str(gone)]
    assert not gone_state.exists()
    assert here_state.is_dir()


def test_prune_stale_skips_state_dir_without_source(tmp_path):
    stray = state._DIRSTATE_DIR / "stray"
    stray.mkdir(parents=True)

    _, _, pruned_state = state.prune_stale()

    assert pruned_state == []
    assert stray.is_dir()
