# Tests for the pure helper functions in aiab.cli.
#
# Only the host-side filesystem helpers are covered here; the parts that drive
# LXD (containers, devices) need a live LXD and are exercised manually.

from typing import Any

from aiab.cli import _guard_git_repo, _reseed_file, _reseed_tree


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
