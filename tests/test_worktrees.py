# Tests for aiab.worktrees — where a run's worktree lives, and finding the
# ones already there. The scan runs against real git worktrees, since what it
# is really asserting is the on-disk shape git produces.

import subprocess as sp

import pytest

from aiab import worktrees


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit, ready for worktrees."""
    root = tmp_path / "proj"
    root.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", "."], cwd=root, check=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "f").write_text("x\n")
    sp.run(["git", "add", "f"], cwd=root, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def _add(repo, branch):
    sp.run(
        ["git", "worktree", "add", "-q", "-b", branch, worktrees.path_for(".", branch)],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _add_detached(repo, leaf):
    sp.run(
        ["git", "worktree", "add", "-q", "--detach", f"{worktrees.DIR_NAME}/{leaf}"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# path_for
# ---------------------------------------------------------------------------


def test_path_for_is_named_for_the_branch():
    assert (
        worktrees.path_for("/work/proj", "feature/x")
        == "/work/proj/.git/aiab-worktrees/feature/x"
    )


def test_path_for_without_a_branch_is_unique_per_run():
    first = worktrees.path_for("/work/proj", None)
    second = worktrees.path_for("/work/proj", None)
    assert first != second
    assert first.startswith("/work/proj/.git/aiab-worktrees/")


# ---------------------------------------------------------------------------
# existing
# ---------------------------------------------------------------------------


def test_existing_is_empty_without_any_worktrees(repo):
    assert worktrees.existing(repo) == []


def test_existing_is_empty_outside_a_repo(tmp_path):
    assert worktrees.existing(tmp_path) == []


def test_existing_lists_branch_worktrees(repo):
    _add(repo, "topic")
    assert worktrees.existing(repo) == ["topic"]


def test_existing_finds_a_nested_branch_name(repo):
    # 'feature' is an intermediate directory with no .git of its own, so the
    # scan has to walk into it to find the worktree.
    _add(repo, "feature/x")
    assert worktrees.existing(repo) == ["feature/x"]


def test_existing_skips_detached_worktrees(repo):
    # path_for names these after the clock, so there is no branch to resume and
    # offering one would create a branch called 1738000000000000000.
    _add(repo, "topic")
    _add_detached(repo, "1738000000000000000")
    assert worktrees.existing(repo) == ["topic"]


def test_existing_does_not_descend_into_a_worktree(repo):
    # A checkout can contain submodules, whose gitfiles look exactly like a
    # worktree's. Recursion has to stop at the worktree itself.
    _add(repo, "topic")
    nested = repo / worktrees.DIR_NAME / "topic" / "vendor" / "lib"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: /somewhere/else\n")
    assert worktrees.existing(repo) == ["topic"]


def test_existing_ignores_a_bare_directory_with_no_gitfile(repo):
    # Something left behind that is not a worktree must not be offered as one.
    (repo / worktrees.DIR_NAME / "leftover").mkdir(parents=True)
    assert worktrees.existing(repo) == []


def test_existing_survives_a_removed_worktree(repo):
    # `git worktree remove` takes the directory with it, which is the normal end
    # of a session without --worktree-keep.
    _add(repo, "topic")
    sp.run(
        ["git", "worktree", "remove", "--force", worktrees.path_for(".", "topic")],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert worktrees.existing(repo) == []
