# Copyright (C) 2026 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# aiab.cli - the `aiab` command-line front end.
#
# A Click group with a command per verb (run, remove, mount, unmount, net,
# upgrade-templates, list, lxc). The LXD engine (Lxd/Container) lives in
# aiab.lxd, provisioning in aiab.provision, the per-agent data in aiab.agents,
# and the filtering-proxy/idle-stop plumbing in aiab.lifecycle; this module
# just parses arguments and orchestrates. The Lxd connection is built once
# per invocation and passed to commands via ctx.obj.

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click

from . import (
    PROJECT,
    CONTAINER_LOGIN,
    CONTAINER_USER,
    CONTAINER_HOME,
    WORK_PREFIX,
    STATE_MOUNT,
)
from . import agents
from . import lifecycle
from . import lxd
from . import netproxy
from . import profiles
from . import provision
from . import release
from . import state
from . import worktrees

CONFIG_CONTAINER_PATH: str = CONTAINER_HOME  # agent home dir is mounted here

# PATH for the agent process (and so everything it spawns). Without this, lxc
# exec falls back to a default that lacks ~/.local/bin — where the agent
# installers (and tools a setup script installs) put their binaries. Login
# shells reset PATH in /etc/profile and get ~/.local/bin back from the
# /etc/profile.d snippet provisioned into the template (see aiab.provision).
_CONTAINER_PATH: str = (
    f"{CONTAINER_HOME}/.local/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

AGENT_CHOICE = click.Choice(agents.AGENT_NAMES)

# Carries the shared-tree answer (see _resolve_shared_tree) from the outer
# process to the inner one _reexec_under_tmux starts, so the question is asked
# once, on a real terminal, rather than again inside the tmux pane.
_CONCURRENT_DECISION: str = "AIAB_CONCURRENT_DECISION"
_DECISION_WORKTREE: str = "worktree"
_DECISION_CONTINUE: str = "continue"

# The type for every directory-valued parameter. Its only real job is shell
# completion: click derives the completion script from the command tree, and
# this is what makes a DIR parameter offer directories (see docs/install.md).
# Deliberately no exists=True — `aiab remove --for` and `aiab unmount` are
# expected to work on a directory that has already been deleted, to clean up
# the container and records it left behind. A path that exists but is a file
# is still rejected, which is what the old hand-written completions could only
# hint at.
_DIR = click.Path(file_okay=False)


def _agent_command(
    cfg: agents.Agent, agent_args: tuple[str, ...], shell: bool
) -> list[str]:
    """Build the command run interactively inside the session container."""
    if shell:
        # bash doesn't take the agent's arguments; silently dropping them
        # would be confusing (`aiab run --shell claude -- -c 'echo hi'` would
        # just open a shell with no explanation), so reject the combination.
        if agent_args:
            raise click.UsageError("--shell doesn't take agent arguments")
        return ["bash", "-l"]
    cmd_args = list(cfg.extra_args) + list(agent_args)
    return [cfg.command] + cmd_args


def _realdir(path: str | None) -> Path:
    return Path(path).resolve() if path else Path.cwd()


# -- git worktree helpers --
#
# Where they live is aiab.worktrees; this is the creating/removing half.


def _git(
    session: lxd.Container, repo_cwd: str, *args: str
) -> subprocess.CompletedProcess:
    """Run git in the container as the login user, without checking the result."""
    return session.exec(
        ["runuser", "-u", CONTAINER_LOGIN, "--", "git", "-C", repo_cwd, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _worktree_add_args(
    worktree_path: str, branch: str | None, branch_exists: bool
) -> list[str]:
    """The `git worktree add` arguments for a run.

    Three shapes, because git spells them differently: no branch is a detached
    checkout; a new branch needs -b; an existing branch must *not* get -b, which
    fails outright rather than reusing it.
    """
    if branch is None:
        return ["worktree", "add", "--detach", worktree_path]
    if branch_exists:
        return ["worktree", "add", worktree_path, branch]
    return ["worktree", "add", "-b", branch, worktree_path]


def _setup_worktree(
    session: lxd.Container, repo_cwd: str, user: int, branch: str | None = None
) -> str:
    """Create (or re-enter) a git worktree inside the container; return its path.

    Stored under <repo>/.git/aiab-worktrees/. With a branch the worktree is
    checked out on it; without one it is a detached HEAD at the current commit,
    which avoids leaving throwaway branch refs behind.

    A branch is also what makes the result survive: `git worktree remove` drops
    the checkout but never the branch, so committed work outlives the session
    even without --worktree-keep. A detached worktree has no ref keeping its
    commits reachable, so removing it discards them.

    Naming a branch that already exists re-enters it rather than failing, so a
    session can be resumed. git refuses if it is checked out in another
    worktree, which is exactly the "another run already has this branch" case.
    """
    worktree_path = worktrees.path_for(repo_cwd, branch)

    # Verify it's actually a git repo.
    r = session.exec(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_cwd,
        user=user,
        check=False,
        capture_output=True,
    )
    if r.returncode != 0:
        raise click.ClickException(
            f"--worktree requires a git repository, but {repo_cwd} is not one"
        )

    # A leftover directory (--worktree-keep, or a crash before cleanup) is
    # reusable when it really is this branch's worktree; anything else in the
    # way is something we must not silently clobber.
    if (
        branch
        and _git(session, worktree_path, "rev-parse", "--git-dir").returncode == 0
    ):
        head = _git(session, worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
        if head.stdout.strip() == branch:
            print(f"Reusing worktree at container:{worktree_path}", file=sys.stderr)
            return worktree_path
        raise click.ClickException(
            f"{worktree_path} already exists and is not a worktree for "
            f"'{branch}' (it is on '{head.stdout.strip()}'); remove it or pick "
            "another branch name"
        )

    branch_exists = branch is not None and (
        _git(
            session, repo_cwd, "rev-parse", "--verify", f"refs/heads/{branch}"
        ).returncode
        == 0
    )
    add_args = _worktree_add_args(worktree_path, branch, branch_exists)

    r = _git(session, repo_cwd, *add_args)
    if r.returncode != 0:
        # git's own message is the useful one here — an invalid branch name, or
        # a branch another worktree already holds.
        raise click.ClickException(
            (r.stderr or r.stdout).strip() or f"git {' '.join(add_args)} failed"
        )
    print(f"Created worktree at container:{worktree_path}", file=sys.stderr)
    return worktree_path


def _remove_worktree(session: lxd.Container, repo_cwd: str, worktree_path: str) -> None:
    """Remove a worktree created by _setup_worktree."""
    try:
        session.exec(
            [
                "runuser",
                "-u",
                CONTAINER_LOGIN,
                "--",
                "git",
                "-C",
                repo_cwd,
                "worktree",
                "remove",
                "--force",
                worktree_path,
            ]
        )
        print(f"Removed worktree at container:{worktree_path}", file=sys.stderr)
    except subprocess.CalledProcessError:
        print(
            f"Warning: could not remove worktree {worktree_path}",
            file=sys.stderr,
        )


def _drop_stale_mounts(container: lxd.Container) -> None:
    """Remove disk devices whose host source no longer exists.

    A container with a bind mount to a since-moved/removed host directory
    refuses to start under LXD. Since ``remove`` is about to delete the
    container anyway, dropping those devices is safe and unblocks startup so
    worktree pruning can still run. The root device and devices whose source
    still exists are left alone.
    """
    for name, dev in list(container.devices().items()):
        if dev.get("type") != "disk" or dev.get("path") == "/":
            continue
        source = dev.get("source")
        if source and not Path(source).exists():
            print(
                f"Dropping stale mount '{name}' (source {source} no longer "
                f"exists on host)",
                file=sys.stderr,
            )
            container.remove_device(name)


def _prune_worktrees(session: lxd.Container, repo_cwd: str, user: int) -> None:
    """Prune stale worktree bookkeeping for a repo (e.g. after a crash)."""
    session.exec(
        ["git", "worktree", "prune"],
        cwd=repo_cwd,
        user=user,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _resolve_shared_tree(container_name: str, work_dir: Path, worktree: bool) -> bool:
    """Return whether to use a worktree, asking about a shared tree if need be.

    Concurrent runs for one directory are supported by design — they share the
    session container, its lock and its proxy (see aiab.lifecycle) — but
    without --worktree they also share one checkout, so two agents edit the
    same files with nothing keeping them apart. That is a footgun rather than a
    feature, and too easy to miss as a printed warning: this runs moments before
    the agent takes over the screen and redraws it. So ask instead, and offer
    the fix as one of the answers rather than as advice to go and re-run.

    Called before stop_when_idle() takes this run's own lock, or the probe would
    find us. Asked by the outer process only — the answer reaches the inner one
    through the environment (see _CONCURRENT_DECISION), which is also how a
    worktree chosen here survives the re-exec, since the flag is not in argv.
    """
    decided = os.environ.get(_CONCURRENT_DECISION)
    if decided is not None:
        return worktree or decided == _DECISION_WORKTREE
    if worktree or not lifecycle.session_in_use(container_name):
        return worktree

    problem = (
        f"Another agent session is already running for {work_dir}.\n"
        "Both would share the one working tree, editing the same files."
    )
    # A worktree needs somewhere to branch from. Testing for .git rather than
    # asking git keeps this on the host (the container isn't up yet); a gitfile
    # counts, since `git worktree add` works in a linked worktree too.
    can_worktree = (work_dir / ".git").exists()

    if not sys.stdin.isatty():
        # Nothing to prompt with. Say it and continue rather than fail: a
        # non-interactive caller may well know exactly what it is doing.
        advice = (
            "Pass --worktree to give this run its own checkout."
            if can_worktree
            else f"{work_dir} is not a git repository, so --worktree is no help here."
        )
        print(f"Warning: {problem}\n{advice}", file=sys.stderr)
        return worktree

    click.echo(problem, err=True)
    if not can_worktree:
        # Only two real answers, so don't offer a third that cannot work.
        click.echo(
            f"{work_dir} is not a git repository, so a worktree is not an option.",
            err=True,
        )
        if click.confirm("Continue anyway?", default=False, err=True):
            return False
        raise click.Abort()

    choice = click.prompt(
        "Run in a new worktree, continue anyway, or exit?",
        type=click.Choice([_DECISION_WORKTREE, _DECISION_CONTINUE, "exit"]),
        default=_DECISION_WORKTREE,
        err=True,
    )
    if choice == "exit":
        raise click.Abort()
    if choice == _DECISION_WORKTREE:
        # --worktree semantics, so say what happens to it: the same surprise as
        # the flag has, but the flag at least had to be typed.
        click.echo(
            "Running in a fresh worktree; it is removed when the agent exits "
            "(--worktree-keep keeps it).",
            err=True,
        )
        return True
    return False


# -- git guard --
#
# Git hooks and several .git/config keys (core.hooksPath, aliases, core.pager,
# core.fsmonitor, filter clean/smudge, ...) are code that runs when *host* git
# touches the repo — i.e. outside the container, on commands as innocuous as
# `git status` or `git diff`. The agent works in the mounted, writable working
# tree, so without protection it could drop such a payload into .git and have
# it fire on the host. The git guard gives the container its own copies of
# .git/hooks (read-write, so in-container hook installs still work — they just
# stay in the container) and .git/config (read-only), seeded from the host's,
# bind-mounted over the repo's real paths. The host's files are shadowed and
# left untouched. Defeating it would need a kernel container escape, the same
# bar as the rest of the sandbox.


def _reseed_file(dst: Path, src: Path) -> None:
    """Copy src onto dst in place, preserving dst's inode if it already exists.

    In place (a truncating write, not an atomic rename) so that a sidecar
    already bind-mounted into a reused, running container reflects the new
    contents — a rename would leave the live mount pointing at the old inode.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _reseed_tree(dst: Path, src: Path) -> None:
    """Refresh dst to mirror src's entries, clearing dst's in place.

    The directory itself is preserved (only its entries are replaced), so a
    live bind mount of it keeps working in a reused container.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for child in dst.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target, follow_symlinks=False)


def _guard_git_repo(
    session: lxd.Container, repo_dir: Path, container_path: str, guard: Path
) -> None:
    """Shadow repo_dir's .git/hooks and .git/config with sidecars under guard.

    repo_dir is a host repo whose .git lives at {container_path}/.git inside the
    container; guard is the host dir holding the per-repo sidecar copies. No-op
    when repo_dir is not a git repository, or when its .git is a gitfile rather
    than a directory (e.g. a linked worktree or submodule checkout mounted
    directly) — there the .git/hooks and .git/config paths don't live where we'd
    mount them, so we skip rather than guard the wrong thing.
    """
    git_dir = repo_dir / ".git"
    if not git_dir.is_dir():
        return

    host_hooks = git_dir / "hooks"
    if host_hooks.is_dir():
        side_hooks = guard / "hooks"
        _reseed_tree(side_hooks, host_hooks)
        session.add_config_overlay(
            side_hooks,
            f"{container_path}/.git/hooks",
            container_user=CONTAINER_USER,
        )

    host_config = git_dir / "config"
    if host_config.is_file():
        side_config = guard / "config"
        _reseed_file(side_config, host_config)
        session.add_config_overlay(
            side_config,
            f"{container_path}/.git/config",
            container_user=CONTAINER_USER,
            readonly=True,
        )


def _setup_git_guard(
    session: lxd.Container, work_dir: Path, container_cwd: str
) -> None:
    """Shadow the work dir repo's .git/hooks and .git/config (see git guard)."""
    _guard_git_repo(session, work_dir, container_cwd, state.git_guard_dir(work_dir))


def _guard_mount(
    container: lxd.Container, for_dir: Path, source: Path, container_path: str
) -> None:
    """Git-guard a read-write mounted repo so the agent can't plant hooks or
    config that fire on the host. Sidecars live in a per-mount subdir of
    for_dir's guard dir, keyed by the mount source so mounts don't collide.
    """
    _guard_git_repo(
        container, source, container_path, state.git_guard_dir(for_dir, source)
    )


# -- tmux control plane --
#
# `aiab run` on a terminal gets wrapped in tmux: the agent in the main pane,
# `aiab monitor` (the host-side control plane, see aiab.netwatch /
# aiab.monitor_tui) in a small pane below it. Two layers, both thin:
#
#  * outside tmux, run() execs `tmux new-session` re-running the very same
#    aiab command line — the re-run sees TMUX set, so the recursion ends;
#  * inside tmux (whether our own session or one the user already had),
#    _monitor_pane() splits off the monitor pane for the duration of the
#    agent session and kills it afterwards.
#
# The session is named after the container (_tmux_group), so a second run for
# the same directory can find the first instead of starting an unrelated
# session. It joins as a tmux *session group*: one shared window list, one
# window per agent, but a per-session current-window pointer, so each terminal
# sees every agent for the directory and can look at whichever it likes
# without dragging anyone else's view along.
#
# The monitor's presence is also what switches the proxy from fail-fast 403s
# to parking unknown hosts for an interactive decision.


def _self_argv0() -> str:
    """An absolute path for re-invoking aiab (sys.argv[0] may be a bare name)."""
    argv0 = sys.argv[0]
    if os.sep in argv0:
        return str(Path(argv0).resolve())
    return shutil.which(argv0) or argv0


def _tmux_group(container_name: str) -> str:
    """The tmux session-group name for a session container.

    Keyed by the container, which is exactly the granularity that shares a
    working tree: two runs that land in the same container belong in one group,
    and a different agent or isolated profile (a different container) gets its
    own.
    """
    return f"aiab-{container_name}"


def _tmux_window_name(agent: str, branch: str | None) -> str:
    """Label for a run's tmux window: the agent, and its branch if it has one.

    This is what makes the windows of parallel runs tellable apart in the window
    list. '@' rather than ':' as the separator, since ':' is what splits session
    from window in a tmux target string.
    """
    return f"{agent}@{branch}" if branch else agent


def _tmux_sessions() -> list[tuple[str, str]]:
    """Every live tmux session as (name, group); empty if there is no server.

    A lone session reports an empty group, so a group's members are the
    sessions whose group matches *or* the session named after the group
    itself — see _tmux_group_member.
    """
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_group}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []  # no server running yet, so no sessions
    out = []
    for line in r.stdout.splitlines():
        name, _, group = line.partition("\t")
        if name:
            out.append((name, group))
    return out


def _tmux_group_member(group: str, sessions: list[tuple[str, str]]) -> str | None:
    """Name a live session of `group` to attach a new one to, or None.

    Any member will do: joining via a member's name puts the new session in
    that member's group, so the group outlives the session that started it
    (the first run can exit while later ones keep the group alive under
    generated names).
    """
    for name, member_group in sessions:
        if member_group == group or (not member_group and name == group):
            return name
    return None


def _tmux_session_name(group: str, taken: set[str]) -> str:
    """The group name itself if free, else the first free '<group>-N'."""
    if group not in taken:
        return group
    n = 2
    while f"{group}-{n}" in taken:
        n += 1
    return f"{group}-{n}"


def _tmux_window_commands(session: str) -> list[str] | None:
    """The start command of every window in `session`, or None if unknown.

    Used to tell whether a `new-session -t` actually joined anything: tmux
    does *not* fail when the target has exited since we looked it up. It
    quietly creates a fresh group with a default shell window instead, which
    reports an empty start command — every window aiab makes runs an explicit
    wrapper script, so "no start command" means tmux invented it.

    Deliberately not decided by "our session has no group siblings": the member
    we joined can exit a moment later, which looks identical from the group's
    side but leaves us legitimately holding *its* agent window. Testing the
    window itself can't confuse the two, and never risks killing a live agent.
    """
    r = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{pane_start_command}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.splitlines()


def _tmux_joined_nothing(commands: list[str] | None) -> bool:
    """True when a `new-session -t` shared no windows with anyone.

    `commands` is _tmux_window_commands() for the session just created: all
    empty means every window in it is one tmux made for us, so the target was
    already gone. Neither None (the query failed) nor an empty list (a session
    always has at least one window, so this means we cannot see it) is that
    case — leave the session alone rather than discard one we can't read.
    """
    if not commands:
        return False
    return not any(commands)


def _reexec_under_tmux(group: str, window: str, worktree: bool) -> None:
    """Run this aiab invocation inside a tmux session named for its container.

    The first run for a container creates the session; a concurrent one joins
    it as a *grouped* session, which shares the window list but keeps its own
    current-window pointer — so each terminal sees every agent running for the
    directory and can look at whichever it likes without moving anyone else's
    view.

    Stderr from the inner process is teed to a temp file while still showing
    live in the pane.  After tmux closes, if the inner exit code was non-zero
    and the log has content, the log is printed to the outer terminal so that
    errors do not disappear with the pane.

    Mouse mode is set on our session only (no -g, so a user's own tmux
    sessions and config are untouched): clicks then switch pane focus, and
    reach the watch pane's allow/deny buttons even while the agent pane has
    focus.
    """
    inner = shlex.join([_self_argv0(), *sys.argv[1:]])

    # Pre-create the log and rc files so we know the paths before launching.
    log_fd, log_path = tempfile.mkstemp(prefix="aiab-stderr-", suffix=".log")
    os.close(log_fd)
    rc_fd, rc_path = tempfile.mkstemp(prefix="aiab-rc-", suffix=".txt")
    os.close(rc_fd)

    sessions = _tmux_sessions()
    member = _tmux_group_member(group, sessions)
    name = _tmux_session_name(group, {s for s, _ in sessions})

    # Write a bash wrapper that tees stderr and records the exit code, both to
    # files the outer process can read after tmux exits.
    #
    # The trailing kill-session is what returns the terminal. A lone session
    # dies with its only window, but a grouped one shares the *other* runs'
    # windows, so when our agent exits tmux would simply show somebody else's
    # agent and leave this terminal attached. Killing our own session detaches
    # just us; the other members and their agents are untouched. It runs after
    # the exit code is recorded, since it takes this script's pane with it.
    #
    # _CONCURRENT_DECISION passes on the shared-tree answer, so the inner
    # process neither asks again nor loses a worktree chosen here — it is not in
    # argv, and appending it there would land after any `--` passthrough
    # separator and go to the agent instead (see _resolve_shared_tree).
    decision = _DECISION_WORKTREE if worktree else _DECISION_CONTINUE
    script_fd, script_path = tempfile.mkstemp(prefix="aiab-run-", suffix=".sh")
    try:
        os.write(
            script_fd,
            (
                "#!/bin/bash\n"
                f"{_CONCURRENT_DECISION}={decision} {inner} "
                f"2> >(tee {shlex.quote(log_path)} >&2)\n"
                f"echo $? > {shlex.quote(rc_path)}\n"
                f"tmux kill-session -t {shlex.quote(name)} 2>/dev/null\n"
            ).encode(),
        )
    finally:
        os.close(script_fd)
    os.chmod(script_path, 0o700)

    if member is not None:
        # Detached first, so our window exists before this terminal attaches and
        # there is no flicker through somebody else's agent.
        subprocess.run(["tmux", "new-session", "-d", "-t", member, "-s", name])
        if _tmux_joined_nothing(_tmux_window_commands(name)):
            # The member exited between the lookup above and this join, so tmux
            # gave us a fresh group with a default window rather than sharing
            # anything (see _tmux_window_commands). There is nothing to join:
            # discard it and start the group cleanly, which also gets the group
            # name right for the runs after us. Safe to kill — the only window
            # is tmux's own, and ours does not exist yet.
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            member = None

    if member is None:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-s",
                name,
                "-n",
                window,
                "-c",
                os.getcwd(),
                script_path,
                ";",
                "set-option",
                "mouse",
                "on",
            ]
        )
    else:
        subprocess.run(["tmux", "set-option", "-t", name, "mouse", "on"])
        subprocess.run(
            [
                "tmux",
                "new-window",
                "-t",
                name,
                "-n",
                window,
                "-c",
                os.getcwd(),
                script_path,
            ]
        )
        subprocess.run(["tmux", "attach-session", "-t", name])
    # Read the inner process's exit code (tmux does not propagate it).
    inner_rc = 1
    finished = False
    try:
        inner_rc = int(Path(rc_path).read_text().strip())
        finished = True
    except (OSError, ValueError):
        pass
    finally:
        Path(rc_path).unlink(missing_ok=True)

    # Only remove the wrapper once it has actually finished. tmux also returns
    # here when the client *detaches* (C-b d) with the agent still running, and
    # bash may not have read the whole script yet — deleting it then can break
    # a live session. Leaks a small file in that case, which beats that.
    if finished:
        os.unlink(script_path)

    # Display any captured stderr in the outer terminal on unclean exit.
    try:
        stderr_log = Path(log_path).read_text()
        if inner_rc != 0 and stderr_log.strip():
            sys.stderr.write("\n--- stderr from failed aiab session ---\n")
            sys.stderr.write(stderr_log)
            if not stderr_log.endswith("\n"):
                sys.stderr.write("\n")
            sys.stderr.write("---\n")
    finally:
        Path(log_path).unlink(missing_ok=True)

    sys.exit(inner_rc)


@contextlib.contextmanager
def _monitor_pane(work_dir: Path, container_name: str, enabled: bool) -> Iterator[None]:
    """Show `aiab monitor` in a tmux pane below us for the duration.

    The pane is told which session container it is steering, so its mounts
    view edits that container's live mounts. Best-effort: if the split fails
    (ancient tmux, weird layout), the agent session proceeds without a control
    plane and the proxy stays fail-fast.
    """
    pane_id = None
    if enabled:
        monitor_cmd = shlex.join(
            [
                _self_argv0(),
                "monitor",
                f"--for={work_dir}",
                f"--container={container_name}",
            ]
        )
        r = subprocess.run(
            # -d keeps focus on the agent pane; -P -F prints the new pane's
            # id so we can kill exactly that pane (and not whatever else the
            # user has since opened) when the agent exits.
            [
                "tmux",
                "split-window",
                "-d",
                "-l",
                "10",
                "-P",
                "-F",
                "#{pane_id}",
                monitor_cmd,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            pane_id = r.stdout.strip() or None
        else:
            print(
                f"Warning: could not open monitor pane: {r.stderr.strip()}",
                file=sys.stderr,
            )
    try:
        yield
    finally:
        if pane_id:
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


class _Command(click.Command):
    """A Command that prepares the LXD connection before invoking its body.

    The 'aiab' project is created lazily on first use (see lxd.run), so this
    just builds the connection and stashes it on the context.
    """

    def invoke(self, ctx: click.Context) -> Any:
        conn = lxd.Lxd(PROJECT)
        ctx.obj = conn
        return super().invoke(ctx)


class _Group(click.Group):
    command_class = _Command


@click.group(cls=_Group)
def main() -> None:
    """Run coding agents in disposable per-directory LXD containers."""


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _record_runtime_mounts(
    work_dir: Path, add_mount: tuple[str, ...], add_mount_rw: tuple[str, ...]
) -> None:
    """Persist mounts supplied on one `aiab run` for future sessions too."""
    for d in add_mount:
        state.set_mount(work_dir, d, readonly=True)
    for d in add_mount_rw:
        state.set_mount(work_dir, d, readonly=False)


def _apply_session_mounts(
    session: lxd.Container,
    cfg: agents.Agent,
    work_dir: Path,
    add_mount: tuple[str, ...],
    add_mount_rw: tuple[str, ...],
) -> tuple[str, list[tuple[Path, str, bool]]]:
    """Mount the source dir, recorded extras, dirstate, and config overlays."""
    container_cwd = session.add_device(work_dir, work_prefix=WORK_PREFIX)

    _record_runtime_mounts(work_dir, add_mount, add_mount_rw)
    applied_mounts = _apply_recorded_mounts(session, work_dir)

    # Mount the directory's persistent state dir (shared by all agents for this
    # directory). /setup-container maintains the setup script there, so it
    # survives container recreation.
    session.add_config_overlay(
        state.dir_state_dir(work_dir), STATE_MOUNT, container_user=CONTAINER_USER
    )

    for host_path, overlay_path in cfg.overlays:
        if host_path.exists():
            session.add_config_overlay(
                host_path, overlay_path, container_user=CONTAINER_USER
            )
    return container_cwd, applied_mounts


def _apply_network_policy(
    conn: lxd.Lxd,
    session: lxd.Container,
    work_dir: Path,
    agent: str,
    profile: str | None = None,
) -> dict[str, str]:
    """Apply the recorded network policy and return proxy env vars."""
    policy = state.get_network(work_dir)
    nic_names = conn.profile_nic_names()
    if policy["mode"] != state.MODE_RESTRICTED:
        session.unmask_profile_devices(nic_names)
        session.remove_device("netproxy")
        return {}

    session.mask_profile_devices(nic_names)
    sock_name = lifecycle.ensure_proxy(session, work_dir, agent, profile)
    session.add_proxy_device(
        "netproxy",
        listen=f"tcp:127.0.0.1:{netproxy.PROXY_PORT}",
        connect=f"unix:{sock_name}",
    )
    proxy_url = f"http://127.0.0.1:{netproxy.PROXY_PORT}"
    env = {
        var: proxy_url
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
    print(
        "Network is restricted; use 'aiab net' to inspect or adjust.",
        file=sys.stderr,
    )
    return env


def _apply_git_guard(
    session: lxd.Container,
    work_dir: Path,
    container_cwd: str,
    applied_mounts: list[tuple[Path, str, bool]],
) -> None:
    """Shadow host-facing git config/hooks for writable mounted repos."""
    _setup_git_guard(session, work_dir, container_cwd)
    for source, mount_cwd, readonly in applied_mounts:
        if not readonly:
            _guard_mount(session, work_dir, source, mount_cwd)


def _session_env(
    session: lxd.Container,
    cfg: agents.Agent,
    proxy_env: dict[str, str],
    work_dir: Path,
    agent: str,
    profile_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for the agent or shell process inside the container.

    A profile's vars go in first and the directory's recorded `aiab env` vars
    on top, so the precedence is dir > profile: a profile is a reusable
    default, and a variable set for one directory is the more specific
    statement. Both are then overlaid with HOME, PATH, the network-proxy vars
    and the Wayland socket, which aiab manages and which stay authoritative.
    """
    env = dict(profile_env or {})
    env.update(state.get_env(work_dir, agent))
    env["HOME"] = CONTAINER_HOME
    env["PATH"] = _CONTAINER_PATH
    env.update(proxy_env)
    if cfg.wayland:
        env.update(session.mount_wayland(CONTAINER_USER))
    return env


def _resolve_profile(name: str | None, agent: str) -> profiles.Profile | None:
    """Look up a --profile name and check it applies to the agent being run.

    A profile that doesn't list the agent is an error rather than a silent
    no-op: `--profile openrouter copilot` asks for something that can't work,
    and quietly ignoring it would look like it had been applied.
    """
    if name is None:
        return None
    profile = profiles.get(name)
    if profile is None:
        known = ", ".join(profiles.names()) or "none"
        raise click.ClickException(f"no profile '{name}' (known: {known})")
    if not profiles.applies_to(profile, agent):
        scope = ", ".join(profile.get("agents") or [])
        raise click.ClickException(
            f"profile '{name}' applies to {scope}, not '{agent}'"
        )
    return profile


@main.command()
@click.argument("agent", type=AGENT_CHOICE)
@click.option(
    "--for",
    "for_dir",
    metavar="DIR",
    type=_DIR,
    default=None,
    help="run the agent for DIR (default: current directory)",
)
@click.option(
    "--add-mount",
    "add_mount",
    metavar="DIR",
    type=_DIR,
    multiple=True,
    help="mount DIR read-only and record it for this directory (repeatable)",
)
@click.option(
    "--add-mount-rw",
    "add_mount_rw",
    metavar="DIR",
    type=_DIR,
    multiple=True,
    help="mount DIR read-write and record it for this directory (repeatable)",
)
@click.option(
    "--base",
    "base_release",
    metavar="RELEASE",
    default=None,
    help="build/use Ubuntu RELEASE (e.g. 22.04, jammy or devel) and record it "
    "for this directory",
)
@click.option(
    "--profile",
    "profile_name",
    metavar="NAME",
    default=None,
    help="apply the named profile for this run (see `aiab profile list`)",
)
@click.option(
    "--worktree",
    is_flag=True,
    help="run the agent in a fresh git worktree (detached at HEAD)",
)
@click.option(
    "--worktree-keep",
    is_flag=True,
    help="keep the worktree after the agent exits (implies --worktree)",
)
@click.option(
    "--worktree-branch",
    "worktree_branch",
    metavar="BRANCH",
    default=None,
    help="run in a worktree on BRANCH, creating it if needed (implies "
    "--worktree); the branch outlives the session even without --worktree-keep",
)
@click.option(
    "--no-git-guard",
    "no_git_guard",
    is_flag=True,
    help="don't shadow the repo's .git/hooks and .git/config (see git guard)",
)
@click.option(
    "--shell",
    is_flag=True,
    help="open an interactive shell instead of running the agent",
)
@click.option(
    "--no-tmux",
    "no_tmux",
    is_flag=True,
    help="don't wrap the session in tmux with an 'aiab monitor' pane",
)
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
def run(
    conn: lxd.Lxd,
    agent: str,
    for_dir: str | None,
    add_mount: tuple[str, ...],
    add_mount_rw: tuple[str, ...],
    base_release: str | None,
    profile_name: str | None,
    worktree: bool,
    worktree_keep: bool,
    worktree_branch: str | None,
    no_git_guard: bool,
    shell: bool,
    no_tmux: bool,
    agent_args: tuple[str, ...],
) -> None:
    """Run an agent in a container for a directory.

    Anything after `--` is passed straight through to the agent. When tmux
    is available, the session runs under tmux with an `aiab monitor` control
    pane below the agent; --no-tmux opts out.
    """
    # --worktree-keep and --worktree-branch both imply --worktree.
    if worktree_keep or worktree_branch:
        worktree = True
    cfg = agents.get(agent)
    profile = _resolve_profile(profile_name, agent)
    isolated = bool(profile.get("isolated")) if profile else False
    # An isolated profile forks the agent's identity: its own credential store
    # and its own session container, so its credentials and environment can't
    # mix with the plain agent's. The *template* container stays keyed by the
    # agent alone — a profile never changes how the agent is installed.
    home_key = profiles.home_key(agent, profile_name, isolated)
    container_prefix = profiles.container_prefix(agent, profile_name, isolated)
    config_host_dir = lxd.agent_home_dir(home_key)

    work_dir = _realdir(for_dir)

    # --base records the directory's Ubuntu release (like --add-mount records a
    # mount), then uses it below; without it we use the directory's recorded
    # base, or the default.
    if base_release:
        try:
            state.set_base(work_dir, release.normalize(base_release))
        except ValueError as e:
            raise click.ClickException(str(e))
    dir_base = state.get_base(work_dir)

    # The session container's name is a pure function of the directory and the
    # prefix, so it is known before any LXD call — which both of the next two
    # steps need, and neither should pay an LXD round-trip for.
    session_name = lxd.container_name_for_dir(work_dir, container_prefix)

    worktree = _resolve_shared_tree(session_name, work_dir, worktree)

    # Wrap sessions in tmux (see the tmux control plane section).
    # Inside tmux already — ours or the user's — _watch_pane below splits the
    # current window instead, so this re-exec only fires on a bare terminal.
    use_tmux = not no_tmux and sys.stdin.isatty() and shutil.which("tmux") is not None
    if use_tmux and "TMUX" not in os.environ:
        # does not return
        _reexec_under_tmux(
            _tmux_group(session_name),
            _tmux_window_name(agent, worktree_branch),
            worktree,
        )

    # One-time prepare hooks (opencode's permissive config; a built-in
    # profile's credential setup, e.g. the OpenRouter key prompt). Both run
    # against the credential store this session will actually use.
    if cfg.prepare:
        cfg.prepare(config_host_dir)
    if profile_name:
        profile_prepare = profiles.PREPARE.get(profile_name)
        if profile_prepare:
            profile_prepare(config_host_dir)

    base = conn.container(release.base_container_name(agent, dir_base))
    session = conn.container_for_dir(work_dir, container_prefix)

    # A session cloned from a different base (the directory's base changed since
    # it was created) is discarded so it re-clones from the right template.
    # Sessions made before bases existed carry no marker; treat them as default.
    if (
        session.exists()
        and (session.get_config("user.aiab_base") or release.DEFAULT_BASE) != dir_base
    ):
        print(
            f"Directory base is now {dir_base}; rebuilding '{session.name}' ...",
            file=sys.stderr,
        )
        _destroy_session(session)

    if not base.exists():
        provision.provision_base(
            base,
            image=release.image_for(dir_base),
            # Always the *agent's* own home, never a profile's: the template is
            # shared by every profile, so building it under an isolated
            # profile's credential store would hand that store to plain runs
            # too. Sessions that need a different one are repointed below.
            config_host_dir=lxd.agent_home_dir(agent),
            config_container_path=CONFIG_CONTAINER_PATH,
            config_device_name=f"{agent}config",
            install_cmds=cfg.install_cmds,
            container_user=CONTAINER_USER,
        )
    # The session lock is held from before the container starts, so a pending
    # stopper from an earlier session can't stop it out from under us mid-setup.
    with lifecycle.stop_when_idle(session):
        session.ensure_started(base)
        # Record the base this session was cloned from, so a later base change
        # for the directory is detected and triggers a rebuild (above).
        session.set_config("user.aiab_base", dir_base)
        # An isolated profile's session inherits the template's config device,
        # which points at the agent's own home; repoint it at the profile's
        # store. Done every run rather than at creation so it self-heals, and
        # recorded so `aiab list` can say which profile a container belongs to.
        if isolated:
            session.set_device_source(f"{agent}config", config_host_dir)
            session.set_config("user.aiab_profile", profile_name or "")
        session.apply_limits(**state.get_limits(work_dir))

        run_cmd = _agent_command(cfg, agent_args, shell)
        container_cwd, applied_mounts = _apply_session_mounts(
            session, cfg, work_dir, add_mount, add_mount_rw
        )
        proxy_env = _apply_network_policy(conn, session, work_dir, agent, profile_name)

        # If --worktree was requested, create one inside the repo and use it
        # as the agent's working directory instead of the repo root.
        agent_cwd = container_cwd
        if worktree:
            agent_cwd = _setup_worktree(
                session, container_cwd, CONTAINER_USER, worktree_branch
            )

        # Shadow the repo's .git/hooks and .git/config so the agent can't plant
        # code there that would run on the *host*. Done after the worktree
        # setup above so aiab's own git commands aren't subject to the
        # read-only config; the agent/shell session below is. A worktree shares
        # the main repo's .git/hooks and .git/config, so guarding container_cwd
        # (the repo root) covers it too.
        # Read-write mounts can themselves be git repos, so guard their .git
        # too; read-only mounts can't be written, so they need no guard.
        if not no_git_guard:
            _apply_git_guard(session, work_dir, container_cwd, applied_mounts)

        env = _session_env(
            session,
            cfg,
            proxy_env,
            work_dir,
            agent,
            profile.get("env") if profile else None,
        )
        with _monitor_pane(work_dir, session.name, enabled=use_tmux):
            rc = session.run_interactive(
                run_cmd,
                cwd=agent_cwd,
                user=CONTAINER_USER,
                group=CONTAINER_USER,
                env=env,
            )
        if worktree and not worktree_keep:
            _remove_worktree(session, container_cwd, agent_cwd)
    sys.exit(rc)


# --------------------------------------------------------------------------
# remove
# --------------------------------------------------------------------------


def _destroy_session(session: lxd.Container) -> None:
    """Tear down a session container and its proxy (e.g. to rebuild it).

    Unlike `aiab remove`, this skips worktree pruning — the caller is about to
    re-create the container, and the work dir lives on the host either way.
    """
    if session.status() == "RUNNING":
        lifecycle.stop_proxy(session.name)
        session.remove_device("netproxy")
        session.stop(timeout=30)
    session.delete()
    lifecycle.stop_proxy(session.name)


@main.command()
@click.argument("agent", type=AGENT_CHOICE)
@click.option(
    "--for",
    "for_dir",
    metavar="DIR",
    type=_DIR,
    default=None,
    help="target the container for DIR (default: current directory)",
)
@click.option(
    "--profile",
    "profile_name",
    metavar="NAME",
    default=None,
    help="target the container the named profile runs in",
)
@click.pass_obj
def remove(
    conn: lxd.Lxd, agent: str, for_dir: str | None, profile_name: str | None
) -> None:
    """Delete the session container for a directory.

    The base/template container is left intact, so the next run clones a fresh
    one quickly. Any leftover git worktrees created by --worktree are pruned
    from the host directory before deleting the container. An isolated profile
    runs in its own container, so removing that one needs --profile.
    """
    work_dir = _realdir(for_dir)
    profile = _resolve_profile(profile_name, agent)
    isolated = bool(profile.get("isolated")) if profile else False
    if profile_name and not isolated:
        print(
            f"Note: profile '{profile_name}' is not isolated; removing the "
            f"shared {agent} container.",
            file=sys.stderr,
        )
    session = conn.container_for_dir(
        work_dir, profiles.container_prefix(agent, profile_name, isolated)
    )
    if not session.exists():
        print(f"No container '{session.name}' to remove.", file=sys.stderr)
        return
    # Prune stale worktrees before deleting — only possible if the container
    # is running (exec needs a live container) and the host working directory
    # still exists. Start the container temporarily if needed, but first drop
    # any bind mounts whose host source has disappeared: a missing source
    # makes LXD refuse to start. If the working directory itself is gone, or
    # the container still won't start after dropping stale mounts, skip prune
    # and fall through to delete (which works on a stopped container).
    can_prune = work_dir.is_dir()
    was_stopped = session.status() != "RUNNING"
    if was_stopped and can_prune:
        _drop_stale_mounts(session)
        try:
            session.start()
        except subprocess.CalledProcessError:
            print(
                f"Container '{session.name}' would not start; skipping "
                f"worktree prune and deleting directly.",
                file=sys.stderr,
            )
            can_prune = False
    if can_prune:
        container_cwd = session.add_device(work_dir, work_prefix=WORK_PREFIX)
        _prune_worktrees(session, container_cwd, CONTAINER_USER)
        if was_stopped:
            session.stop(timeout=30)
    print(f"Removing container '{session.name}' ...", file=sys.stderr)
    session.delete()
    lifecycle.stop_proxy(session.name)  # in case a crashed session left one behind
    print(f"Removed container '{session.name}'.", file=sys.stderr)


# --------------------------------------------------------------------------
# mount / unmount
# --------------------------------------------------------------------------


def _agent_containers(
    conn: lxd.Lxd, for_dir: Path
) -> Iterator[tuple[str, lxd.Container]]:
    """Yield (prefix, Container) for every existing session container of a dir.

    Covers isolated-profile containers as well as plain agent containers
    (via profiles.session_prefixes) — otherwise mount/unmount would silently
    skip a running `claude-openrouter` session, and unmount in particular
    would leave a stale device on it since nothing else would ever remove it.
    """
    for agent in agents.AGENT_NAMES:
        for prefix in profiles.session_prefixes(agent):
            container = conn.container_for_dir(for_dir, prefix)
            if container.exists():
                yield prefix, container


def _apply_recorded_mounts(
    container: lxd.Container, for_dir: Path
) -> list[tuple[Path, str, bool]]:
    """Add every mount recorded for for_dir to a container.

    Skips (with a warning) any whose source no longer exists, so a recreated
    container still comes up. Returns the applied mounts as
    (source, container_path, readonly) tuples, so the caller can git-guard the
    read-write ones (see _guard_mount).
    """
    applied: list[tuple[Path, str, bool]] = []
    for m in state.get_mounts(for_dir):
        source = Path(m["source"])
        if not source.is_dir():
            print(
                f"Warning: recorded mount {m['source']} not found; skipping",
                file=sys.stderr,
            )
            continue
        container_path = container.add_device(
            source, work_prefix=WORK_PREFIX, readonly=m["readonly"]
        )
        applied.append((source, container_path, m["readonly"]))
    return applied


@main.command()
@click.option(
    "--for",
    "for_dir",
    metavar="DIR",
    type=_DIR,
    default=None,
    help="target the containers for DIR (default: current directory)",
)
@click.option(
    "--ro/--rw",
    "readonly",
    default=True,
    help="mount read-only (the default) or read-write",
)
@click.argument("dirs", nargs=-1, required=True, metavar="DIR...", type=_DIR)
@click.pass_obj
def mount(
    conn: lxd.Lxd, for_dir: str | None, readonly: bool, dirs: tuple[str, ...]
) -> None:
    """Mount extra directories into a directory's containers."""
    target = _realdir(for_dir)
    paths = [Path(p).resolve() for p in dirs]

    # Persist for the directory first, so a later run / another agent (and a
    # recreated container) get the same mounts.
    for path in paths:
        state.set_mount(target, path, readonly=readonly)

    found = False
    for prefix, container in _agent_containers(conn, target):
        found = True
        print(f"=== {prefix} ({container.name}) ===", file=sys.stderr)
        if container.status() != "RUNNING":
            print(
                "Note: container is not running; mounts apply when it next " "starts.",
                file=sys.stderr,
            )
        for path in paths:
            container_path = container.add_device(
                path, work_prefix=WORK_PREFIX, readonly=readonly
            )
            # Guard read-write mounts that are git repos, mirroring `aiab run`,
            # so the agent can't plant host-firing hooks/config in them.
            if not readonly:
                _guard_mount(container, target, path, container_path)
    if not found:
        print(
            "No containers exist for this directory yet; the mounts are "
            "recorded and will apply when you next run an agent here.",
            file=sys.stderr,
        )


@main.command()
@click.option(
    "--for",
    "for_dir",
    metavar="DIR",
    type=_DIR,
    default=None,
    help="target the containers for DIR (default: current directory)",
)
@click.argument("dirs", nargs=-1, required=True, metavar="DIR...", type=_DIR)
@click.pass_obj
def unmount(conn: lxd.Lxd, for_dir: str | None, dirs: tuple[str, ...]) -> None:
    """Remove extra directory mounts from a directory's containers."""
    target = _realdir(for_dir)
    paths = [Path(p).resolve() for p in dirs]

    # Drop from the persistent record so it isn't replayed on the next run.
    for path in paths:
        if not state.remove_mount(target, path):
            print(f"Not recorded: {path}", file=sys.stderr)

    for prefix, container in _agent_containers(conn, target):
        print(f"=== {prefix} ({container.name}) ===", file=sys.stderr)
        for path in paths:
            if container.remove_dir_device(path):
                print(f"Unmounted {path}", file=sys.stderr)
            else:
                print(f"Not mounted: {path}", file=sys.stderr)


# --------------------------------------------------------------------------
# net
# --------------------------------------------------------------------------


def _parse_duration(text: str) -> float:
    """Parse a duration like '90s', '10m', '2h', '1d' into seconds.

    Bare numbers are minutes.
    """
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", text.strip().lower())
    if not m:
        raise click.BadParameter(f"invalid duration {text!r} (try e.g. 10m, 2h)")
    scale = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return float(m.group(1)) * scale


def _format_expiry(expires: float | None) -> str:
    if expires is None:
        return ""
    remaining = expires - time.time()
    if remaining <= 0:
        return " (expired)"
    if remaining < 60:
        return f" (expires in {int(remaining)}s)"
    if remaining < 3600:
        return f" (expires in {int(remaining // 60)}m)"
    return f" (expires in {remaining / 3600:.1f}h)"


def _has_user_rules(policy: state.NetworkPolicy) -> bool:
    return bool(policy["allow"] or policy["deny"] or policy.get("agents"))


def _print_rules(policy: state.NetworkPolicy, indent: str = "  ") -> None:
    """Print a policy's allow/deny rules, all-agents first then per-agent."""
    for a in policy["allow"]:
        print(f"{indent}allow {a['domain']}{_format_expiry(a['expires'])}")
    for d in policy["deny"]:
        print(f"{indent}deny  {d}")
    for name, bucket in sorted(policy.get("agents", {}).items()):
        for a in bucket["allow"]:
            exp = _format_expiry(a["expires"])
            print(f"{indent}allow {a['domain']}{exp} [{name}]")
        for d in bucket["deny"]:
            print(f"{indent}deny  {d} [{name}]")


# A plain Group: the net commands only edit recorded state, so they skip the
# _Command machinery (the LXD connection) the other verbs need.
@main.group(cls=click.Group)
def net() -> None:
    """Manage a directory's network access policy.

    The default mode is restricted: the container gets no direct network
    access, and the agent is routed through a filtering proxy that admits
    only the agent's own API domains plus this directory's allowlist, and
    refuses its denylist. Use 'aiab net open' to opt a directory out. Mode
    changes take full effect the next time an agent starts; allow/deny apply
    immediately to running restricted sessions. 'aiab monitor' opens an
    interactive console that is prompted about unknown domains while the
    agent's request waits.
    """


_for_dir_option = click.option(
    "--for",
    "for_dir",
    metavar="DIR",
    type=_DIR,
    default=None,
    help="target DIR (default: current directory)",
)

_global_option = click.option(
    "--global",
    "global_",
    is_flag=True,
    help="apply to the global list shared by every directory",
)

_agent_option = click.option(
    "--agent",
    "agent",
    type=AGENT_CHOICE,
    default=None,
    help="scope the rule to one agent (default: all agents)",
)


def _net_target(for_dir: str | None, global_: bool) -> Path | None:
    """Resolve the target for a net allow/deny: None when global, else a dir.

    Errors if --global is combined with --for, since they pick rival targets.
    """
    if global_:
        if for_dir is not None:
            raise click.UsageError("--global cannot be combined with --for")
        return None
    return _realdir(for_dir)


def _scope_label(global_: bool, agent: str | None) -> str:
    """A trailing ' for X globally'-style suffix describing a rule's scope."""
    parts = []
    if agent:
        parts.append(f"for {agent}")
    if global_:
        parts.append("globally")
    return (" " + " ".join(parts)) if parts else ""


@net.command()
@_for_dir_option
def status(for_dir: str | None) -> None:
    """Show the network mode and allow/deny lists for a directory."""
    target = _realdir(for_dir)
    policy = state.get_network(target)
    print(f"{target}: {policy['mode']}")
    if policy["mode"] != state.MODE_RESTRICTED:
        return
    print("always allowed (built-in defaults):")
    print(f"  baseline (all agents): {', '.join(agents.BASELINE_DOMAINS)}")
    for name in agents.AGENT_NAMES:
        domains = agents.get(name).api_domains
        print(f"  {name}: {', '.join(domains) if domains else '(none)'}")
    if _has_user_rules(policy):
        print("rules:")
        _print_rules(policy)
    else:
        print("rules: (none)")
    global_policy = state.get_global_network()
    if _has_user_rules(global_policy):
        print("global rules (every directory):")
        _print_rules(global_policy)


@net.command()
@_for_dir_option
def restrict(for_dir: str | None) -> None:
    """Restrict a directory's containers to allowed domains only."""
    target = _realdir(for_dir)
    state.set_network_mode(target, state.MODE_RESTRICTED)
    print(f"Network mode for {target}: restricted", file=sys.stderr)
    print(
        "Takes full effect the next time an agent starts here.",
        file=sys.stderr,
    )


@net.command("open")
@_for_dir_option
def open_(for_dir: str | None) -> None:
    """Restore unrestricted network access for a directory."""
    target = _realdir(for_dir)
    state.set_network_mode(target, state.MODE_OPEN)
    print(f"Network mode for {target}: open", file=sys.stderr)
    print(
        "Running restricted sessions now pass all proxied traffic; direct "
        "network access returns the next time an agent starts here.",
        file=sys.stderr,
    )


@net.command()
@_for_dir_option
@click.option(
    "--duration",
    metavar="TIME",
    default=None,
    help="allow temporarily, e.g. 90s, 10m, 2h (bare numbers are minutes)",
)
@_global_option
@_agent_option
@click.argument("domains", nargs=-1, required=True, metavar="DOMAIN...")
def allow(
    for_dir: str | None,
    duration: str | None,
    global_: bool,
    agent: str | None,
    domains: tuple[str, ...],
) -> None:
    """Allow domains (and their subdomains) for a directory.

    Takes effect immediately in running restricted sessions. Re-allowing a
    domain replaces its expiry, so a plain `allow` makes a temporary grant
    permanent. --global allows the domains in every directory; --agent scopes
    them to one agent (the two axes combine).
    """
    target = _net_target(for_dir, global_)
    expires = time.time() + _parse_duration(duration) if duration else None
    scope = _scope_label(global_, agent)
    for domain in domains:
        state.add_network_allow(target, domain, expires, global_=global_, agent=agent)
        print(f"Allowed {domain}{_format_expiry(expires)}{scope}", file=sys.stderr)
    if (
        target is not None
        and state.get_network(target)["mode"] != state.MODE_RESTRICTED
    ):
        print(
            "Note: network mode here is open; the allowlist only takes "
            "effect after 'aiab net restrict'.",
            file=sys.stderr,
        )


@net.command()
@_for_dir_option
@_global_option
@_agent_option
@click.argument("domains", nargs=-1, required=True, metavar="DOMAIN...")
def deny(
    for_dir: str | None,
    global_: bool,
    agent: str | None,
    domains: tuple[str, ...],
) -> None:
    """Deny domains (and their subdomains) for a directory.

    Drops the domains from the allowlist and records them on the denylist,
    so requests fail fast instead of prompting a watch session. Takes effect
    immediately in running restricted sessions; 'aiab net allow' reverses
    it. The agent's own API domains cannot be denied. --global denies in every
    directory; --agent scopes to one agent (the two axes combine).
    """
    target = _net_target(for_dir, global_)
    scope = _scope_label(global_, agent)
    for domain in domains:
        state.add_network_deny(target, domain, global_=global_, agent=agent)
        print(f"Denied {domain}{scope}", file=sys.stderr)


# --------------------------------------------------------------------------
# base
# --------------------------------------------------------------------------


# A plain Command (no LXD): like the net verbs it only edits recorded state.
# A change is applied lazily — `aiab run` rebuilds the directory's container
# from the new base when it notices the recorded base differs from the one the
# existing container was cloned from.
@main.command(cls=click.Command)
@_for_dir_option
@click.argument("release_arg", required=False, metavar="[RELEASE]")
def base(for_dir: str | None, release_arg: str | None) -> None:
    """Show or set the Ubuntu release a directory's containers are built on.

    With no argument, prints the directory's base release and the default.
    Given a RELEASE (a version like 22.04, a codename like jammy, or 'devel'
    for the release in development), records it for the directory; 'default'
    clears it back to the built-in default. A
    change takes effect the next time an agent starts here — its container is
    rebuilt from the new base then.
    """
    target = _realdir(for_dir)

    if release_arg is None:
        current = state.get_base(target)
        print(f"{target}: {current}")
        if current != release.DEFAULT_BASE:
            print(f"default: {release.DEFAULT_BASE}")
        # The releases still in support, which is what you'd want to build on
        # — but not the limit: any version aiab can find an image for works.
        supported = ", ".join(f"{v} ({c})" for v, c in release.supported())
        print(f"supported releases: {supported}")
        devel = release.devel_version()
        if devel is not None:
            print(f"devel: {devel}")
        return

    if release_arg.strip().lower() == "default":
        canonical = release.DEFAULT_BASE
    else:
        try:
            canonical = release.normalize(release_arg)
        except ValueError as e:
            raise click.ClickException(str(e))
    state.set_base(target, canonical)
    print(f"Base release for {target}: {canonical}", file=sys.stderr)
    print(
        "Takes effect the next time an agent starts here "
        "(its container is rebuilt from the new base).",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------


@main.command(cls=click.Command)
@_for_dir_option
@click.option(
    "--cpu", "cpu", metavar="N", default=None, type=int, help="number of vCPUs"
)
@click.option(
    "--memory", "memory", metavar="SIZE", default=None, help="memory limit, e.g. 8GiB"
)
@click.option("--reset", "reset", is_flag=True, help="reset all limits to defaults")
def limits(
    for_dir: str | None,
    cpu: int | None,
    memory: str | None,
    reset: bool,
) -> None:
    """Show or set the resource limits for a directory's session containers.

    With no options, prints the current limits (or defaults). Specify one or
    more of --cpu, --memory to update individual limits; --reset restores both
    to their built-in defaults. Changes take effect the next time an agent
    starts here (limits are applied on every 'aiab run').

    \b
    Defaults: cpu=4, memory=8GiB.
    """
    target = _realdir(for_dir)

    if reset:
        state.set_limits(target, state.DEFAULT_LIMITS)
        print(f"Resource limits for {target}: reset to defaults", file=sys.stderr)
        return

    if cpu is None and memory is None:
        current = state.get_limits(target)
        print(f"{target}:")
        print(f"  cpu:    {current['cpu']}")
        print(f"  memory: {current['memory']}")
        defs = state.DEFAULT_LIMITS
        if current != defs:
            print(f"defaults: cpu={defs['cpu']} memory={defs['memory']}")
        return

    try:
        if cpu is not None:
            cpu = state.parse_cpu(cpu)
        if memory is not None:
            memory = state.parse_memory(memory)
    except ValueError as e:
        raise click.BadParameter(str(e))

    current = state.get_limits(target)
    if cpu is not None:
        current["cpu"] = cpu
    if memory is not None:
        current["memory"] = memory
    state.set_limits(target, current)

    parts = []
    if cpu is not None:
        parts.append(f"cpu={cpu}")
    if memory is not None:
        parts.append(f"memory={memory}")
    print(f"Resource limits for {target}: {', '.join(parts)}", file=sys.stderr)
    print(
        "Takes effect the next time an agent starts here.",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------
# env
# --------------------------------------------------------------------------


# Variables aiab sets itself when launching an agent (see _session_env); they
# always win over recorded vars, so refuse to record them and avoid the
# surprise of a setting that silently has no effect.
_RESERVED_ENV = ("HOME", "PATH")


# A plain Group (like net): these verbs only edit recorded state. The recorded
# vars are merged into the agent process on the next `aiab run` here.
@main.group(cls=click.Group)
def env() -> None:
    """Manage environment variables injected into a directory's agents.

    Variables are recorded per directory and, by default, apply to every agent
    run there; pass --agent to scope a variable to a single agent (an
    agent-specific value overrides the directory-wide one). Changes take effect
    the next time an agent starts here. HOME, PATH, and the network-proxy and
    Wayland variables are managed by aiab and stay authoritative.
    """


_env_agent_option = click.option(
    "--agent",
    "agent",
    type=AGENT_CHOICE,
    default=None,
    help="scope to one agent (default: all agents in the directory)",
)


def _env_scope(agent: str | None) -> str:
    return f"agent '{agent}'" if agent else "all agents"


@env.command("set")
@_for_dir_option
@_env_agent_option
@click.argument("name")
@click.argument("value")
def env_set(for_dir: str | None, agent: str | None, name: str, value: str) -> None:
    """Set environment variable NAME to VALUE for a directory."""
    if name in _RESERVED_ENV:
        raise click.ClickException(f"{name} is managed by aiab and can't be set here")
    target = _realdir(for_dir)
    state.set_env(target, agent or state.ENV_ALL_AGENTS, name, value)
    print(f"Set {name} for {target} ({_env_scope(agent)})", file=sys.stderr)
    print("Takes effect the next time an agent starts here.", file=sys.stderr)


@env.command("unset")
@_for_dir_option
@_env_agent_option
@click.argument("name")
def env_unset(for_dir: str | None, agent: str | None, name: str) -> None:
    """Remove environment variable NAME for a directory."""
    target = _realdir(for_dir)
    if state.unset_env(target, agent or state.ENV_ALL_AGENTS, name):
        print(f"Unset {name} for {target} ({_env_scope(agent)})", file=sys.stderr)
    else:
        print(
            f"No {name} recorded for {target} ({_env_scope(agent)})",
            file=sys.stderr,
        )


@env.command("list")
@_for_dir_option
def env_list(for_dir: str | None) -> None:
    """Show recorded environment variables for a directory."""
    target = _realdir(for_dir)
    buckets = state.list_env(target)
    if not buckets:
        print(f"{target}: (none)")
        return
    print(f"{target}:")
    # Directory-wide bucket first, then per-agent buckets alphabetically.
    for bucket, variables in sorted(
        buckets.items(), key=lambda kv: (kv[0] != state.ENV_ALL_AGENTS, kv[0])
    ):
        label = "all agents" if bucket == state.ENV_ALL_AGENTS else bucket
        print(f"  {label}:")
        for k, v in sorted(variables.items()):
            print(f"    {k}={v}")


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------


def _print_profile(name: str, profile: profiles.Profile, builtin: bool) -> None:
    """Print one profile as an indented block under its name."""
    tag = " (built-in)" if builtin else ""
    print(f"{name}{tag}")
    if profile.get("description"):
        print(f"  {profile['description']}")
    scope = ", ".join(profile.get("agents") or []) or "any agent"
    print(f"  agents:   {scope}")
    print(f"  isolated: {'yes' if profile.get('isolated') else 'no'}")
    if profile.get("allow"):
        print(f"  allow:    {', '.join(profile['allow'])}")
    for k, v in sorted(profile.get("env", {}).items()):
        print(f"  env:      {k}={v}")


@main.group(cls=click.Group)
def profile() -> None:
    """Manage named bundles of settings selected with `aiab run --profile`.

    A profile carries environment variables, extra domains to allow in
    restricted mode, and whether the session gets its own credential store. It
    is chosen per run rather than recorded against a directory, so the same
    directory can be used with and without one. Built-in profiles ship with
    aiab and can't be edited or removed.
    """


def _parse_env_assignments(assignments: tuple[str, ...]) -> dict[str, str]:
    """Parse repeated NAME=VALUE options into a dict."""
    env: dict[str, str] = {}
    for item in assignments:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise click.ClickException(f"--env expects NAME=VALUE, got '{item}'")
        if name in _RESERVED_ENV:
            raise click.ClickException(
                f"{name} is managed by aiab and can't be set here"
            )
        env[name] = value
    return env


@profile.command("add")
@click.argument("name")
@click.option(
    "--agent",
    "agent_scope",
    type=AGENT_CHOICE,
    multiple=True,
    help="restrict the profile to this agent (repeatable; default: any agent)",
)
@click.option(
    "--isolated/--no-isolated",
    default=False,
    help="give the profile its own credential store and session container",
)
@click.option(
    "--env",
    "env_assignments",
    metavar="NAME=VALUE",
    multiple=True,
    help="environment variable to inject (repeatable)",
)
@click.option(
    "--allow",
    "allow_domains",
    metavar="DOMAIN",
    multiple=True,
    help="domain to allow while in restricted mode (repeatable)",
)
@click.option("--description", default=None, help="one-line description")
def profile_add(
    name: str,
    agent_scope: tuple[str, ...],
    isolated: bool,
    env_assignments: tuple[str, ...],
    allow_domains: tuple[str, ...],
    description: str | None,
) -> None:
    """Record a user profile called NAME, replacing any existing one."""
    if not profiles.valid_name(name):
        raise click.ClickException(
            f"'{name}' isn't a usable profile name — profile names end "
            "up in container names, so use lowercase letters, digits and "
            f"hyphens only, up to {profiles.MAX_NAME_LEN} characters"
        )
    if name in profiles.BUILTIN:
        raise click.ClickException(
            f"'{name}' is a built-in profile and can't be replaced"
        )
    if name in agents.AGENT_NAMES:
        raise click.ClickException(
            f"'{name}' is an agent name; a profile sharing it would "
            "make container names ambiguous"
        )

    new: profiles.Profile = {}
    if description:
        new["description"] = description
    if agent_scope:
        new["agents"] = list(agent_scope)
    if isolated:
        new["isolated"] = True
    env = _parse_env_assignments(env_assignments)
    if env:
        new["env"] = env
    if allow_domains:
        new["allow"] = list(allow_domains)

    state.set_profile(name, dict(new))
    print(f"Recorded profile '{name}'.", file=sys.stderr)
    _print_profile(name, new, builtin=False)


@profile.command("remove")
@click.argument("name")
def profile_remove(name: str) -> None:
    """Remove the user profile called NAME."""
    if name in profiles.BUILTIN:
        raise click.ClickException(
            f"'{name}' is a built-in profile and can't be removed"
        )
    if state.remove_profile(name):
        print(f"Removed profile '{name}'.", file=sys.stderr)
    else:
        print(f"No profile '{name}' recorded.", file=sys.stderr)


@profile.command("list")
def profile_list() -> None:
    """Show every profile, built-in and user-defined."""
    known = profiles.names()
    if not known:
        print("No profiles.", file=sys.stderr)
        return
    for i, name in enumerate(known):
        if i:
            print()
        entry = profiles.get(name)
        assert entry is not None  # names() only yields resolvable names
        _print_profile(name, entry, builtin=name in profiles.BUILTIN)


@profile.command("show")
@click.argument("name")
def profile_show(name: str) -> None:
    """Show one profile in detail."""
    entry = profiles.get(name)
    if entry is None:
        raise click.ClickException(f"no profile '{name}' (see `aiab profile list`)")
    _print_profile(name, entry, builtin=name in profiles.BUILTIN)


# --------------------------------------------------------------------------
# opencode
# --------------------------------------------------------------------------

# opencode merges several config sources, with an OPENCODE_CONFIG file ranking
# above the global config and — unlike an env var or the stored `opencode auth
# login` — able to override a logged-in provider key (config > auth > env).
# `aiab opencode config` keeps a per-directory overlay here and points opencode
# at it via an OPENCODE_CONFIG var injected for the opencode agent (see `aiab
# env`), so a directory can use its own key/model while every other directory
# keeps the shared login. The overlay lives in the directory's state dir,
# mounted at STATE_MOUNT, so it survives container recreation and the agent can
# read it, but it never lands in the repo tree.
_OPENCODE_OVERLAY = "opencode.json"
_OPENCODE_CONFIG_ENV = "OPENCODE_CONFIG"


def _json_set(data: dict, dotted: str, value: Any) -> None:
    """Set a nested key (dotted path) in data, creating intermediate dicts."""
    *parents, last = dotted.split(".")
    cur = data
    for part in parents:
        child = cur.get(part)
        if not isinstance(child, dict):
            child = {}
            cur[part] = child
        cur = child
    cur[last] = value


def _json_unset(data: dict, dotted: str) -> bool:
    """Remove a nested key (dotted path) from data, pruning emptied parents.

    Returns True if the key was present.
    """
    *parents, last = dotted.split(".")
    cur = data
    chain: list[tuple[dict, str]] = []
    for part in parents:
        child = cur.get(part)
        if not isinstance(child, dict):
            return False
        chain.append((cur, part))
        cur = child
    if last not in cur:
        return False
    del cur[last]
    for parent, part in reversed(chain):
        if parent[part]:
            break
        del parent[part]
    return True


def _parse_config_value(raw: str) -> Any:
    """Read a CLI value as JSON (so true/false/numbers work), else as a str."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _write_json(path: Path, data: dict) -> None:
    with path.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# A plain Group (like net/env): only edits recorded state, no LXD.
@main.group(cls=click.Group)
def opencode() -> None:
    """opencode-specific per-directory configuration."""


@opencode.command("config")
@_for_dir_option
@click.option(
    "--unset", "unset", is_flag=True, help="remove PATH instead of setting it"
)
@click.argument("path", required=False)
@click.argument("value", required=False)
def opencode_config(
    for_dir: str | None, unset: bool, path: str | None, value: str | None
) -> None:
    """Show or edit this directory's opencode config overlay.

    With no PATH, prints the overlay. Given a dotted PATH and VALUE, sets that
    key (e.g. `provider.openrouter.options.apiKey sk-or-...`, or `model
    anthropic/claude-sonnet-4-6`) in a per-directory opencode.json and points
    opencode at it via OPENCODE_CONFIG — overriding the shared login for this
    directory only. `--unset PATH` removes a key. VALUE is read as JSON when it
    parses (so true/false/numbers work), otherwise as a string. Takes effect
    the next time opencode starts here.
    """
    target = _realdir(for_dir)
    overlay_path = state.dir_state_dir(target) / _OPENCODE_OVERLAY
    container_path = f"{STATE_MOUNT}/{_OPENCODE_OVERLAY}"

    def _load() -> dict:
        try:
            with overlay_path.open() as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    if path is None:
        if unset:
            raise click.ClickException("--unset needs a PATH")
        data = _load()
        if not data:
            print(f"{target}: (no opencode overlay)")
            return
        print(f"{target} ({container_path}):")
        print(json.dumps(data, indent=2))
        return

    data = _load()

    if unset:
        if value is not None:
            raise click.ClickException("pass either VALUE or --unset, not both")
        if not _json_unset(data, path):
            print(f"No {path} in opencode overlay for {target}", file=sys.stderr)
            return
        if data:
            _write_json(overlay_path, data)
        else:
            # Last key gone: drop the overlay file and the OPENCODE_CONFIG pointer.
            overlay_path.unlink(missing_ok=True)
            state.unset_env(target, "opencode", _OPENCODE_CONFIG_ENV)
        print(f"Unset {path} for {target}", file=sys.stderr)
        print("Takes effect the next time opencode starts here.", file=sys.stderr)
        return

    if value is None:
        raise click.ClickException("setting PATH needs a VALUE (or use --unset)")

    _json_set(data, path, _parse_config_value(value))
    _write_json(overlay_path, data)
    state.set_env(target, "opencode", _OPENCODE_CONFIG_ENV, container_path)
    print(f"Set {path} for {target} (opencode)", file=sys.stderr)
    print("Takes effect the next time opencode starts here.", file=sys.stderr)
    if state.get_network(target)["mode"] == state.MODE_RESTRICTED:
        print(
            "Network here is restricted; if this points opencode at a new "
            "provider, allow its API domain with 'aiab net allow'.",
            file=sys.stderr,
        )


# --container is set by the `aiab run` tmux pane (see _monitor_pane); it names
# the session container whose live mounts the mounts view should edit. Hidden
# because it is plumbing, not something a user types by hand.
_container_option = click.option(
    "--container", "container_name", metavar="NAME", default=None, hidden=True
)


# A plain Command (like the net group): the monitor only reads/writes recorded
# state and drives LXD lazily from the TUI, so it skips the _Command machinery
# (the LXD connection) the container verbs need.
@main.command(cls=click.Command)
@_for_dir_option
@_container_option
def monitor(for_dir: str | None, container_name: str | None) -> None:
    """Open the interactive session control panel for a directory.

    Five tabs in one pane (switch with header buttons or hotkeys 1-5;
    `m`, `p`, and `l` also jump to Mounts, Ports, and Limits):

    \b
      * Network — tails the filtering-proxy logs for this directory's
        containers; while it runs the proxy holds requests for unknown
        domains and prompts here to allow or deny each one (instead of
        refusing them outright), as a row of clickable buttons;
      * Domains — shows recorded domain policy for this directory;
      * Mounts — the directory's recorded extra mounts, each with a
        read-only/read-write toggle and a remove button, plus an input
        (with path completion) to add a new one. Edits are recorded and,
        on a running session, take effect live;
      * Ports — shows recorded port forwarding rules for this directory;
      * Limits — shows recorded resource limits for this directory.

    Each prompt is a row of clickable buttons. `aiab run` opens this in a tmux
    pane automatically; it also works standalone in any terminal.
    """
    # Imported here rather than at module scope so that textual (and the rest
    # of the UI) is only loaded by the one command that draws it, keeping
    # every other `aiab` invocation's startup cheap.
    from . import monitor_tui

    sys.exit(monitor_tui.monitor(_realdir(for_dir), container_name))


# --------------------------------------------------------------------------
# upgrade-templates
# --------------------------------------------------------------------------


@main.command("upgrade-templates")
@click.argument("which", nargs=-1, type=AGENT_CHOICE, metavar="[AGENT]...")
@click.pass_obj
def upgrade_templates(conn: lxd.Lxd, which: tuple[str, ...]) -> None:
    """apt upgrade + reinstall the agent in template containers.

    With no arguments, updates all template containers that currently exist —
    including alternate-release templates (see `aiab base`).
    """
    targets = which or agents.AGENT_NAMES
    instance_names = conn.instances()
    updated = skipped = 0
    for agent in targets:
        cfg = agents.get(agent)
        names = release.base_names_for_agent(agent, instance_names)
        if not names:
            # Report the bare agent so an explicit `which` still prints a skip.
            names = [agent]
        for name in names:
            print(f"=== {name} ===", file=sys.stderr)
            ok = provision.update_template(
                conn.container(name),
                update_cmds=cfg.upgrade_cmds,
                container_user=CONTAINER_USER,
            )
            updated += ok
            skipped += not ok
    print(f"Done: {updated} updated, {skipped} skipped.", file=sys.stderr)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def _fmt_mount(dev: dict[str, str]) -> str:
    line = f"{dev.get('source', '?')} -> {dev.get('path', '?')}"
    if str(dev.get("readonly", "false")).lower() == "true":
        line += " (ro)"
    return line


def _fmt_network(policy: state.NetworkPolicy, masked: bool) -> str:
    """Describe a container's network state for `aiab list`.

    Combines the directory's recorded policy with whether the container's NIC
    is actually masked right now, flagging the gap when a mode change hasn't
    been applied yet (that only happens when an agent next starts).
    """
    line = policy["mode"]
    if policy["mode"] == state.MODE_RESTRICTED:
        n = len(policy["allow"])
        if n:
            line += f" ({n} allowed domain{'s' if n != 1 else ''})"
    if (policy["mode"] == state.MODE_RESTRICTED) != masked:
        line += ", applies on next run"
    return line


def _print_container(conn: lxd.Lxd, name: str, status: str) -> None:
    container = conn.container(name)
    devices = container.devices()
    source = None
    extras = []
    # A 'none' device is the NIC mask restricted mode adds (see aiab net).
    masked = any(dev.get("type") == "none" for dev in devices.values())
    for dev_name, dev in sorted(devices.items()):
        if dev.get("type") != "disk" or not dev_name.startswith("dir-"):
            continue
        if lxd.is_source_device(dev_name, name):
            source = dev
        else:
            extras.append(dev)

    print(f"{name}  [{status}]")
    # Set on isolated-profile sessions only (see the `run` command); read here
    # so `aiab list` can say which profile a container belongs to.
    profile_name = container.get_config("user.aiab_profile")
    if profile_name:
        print(f"  profile: {profile_name}")
    print(f"  source: {_fmt_mount(source)}" if source else "  source: (none)")
    for dev in extras:
        print(f"  mount:  {_fmt_mount(dev)}")
    if source and "source" in source:
        policy = state.get_network(source["source"])
        print(f"  network: {_fmt_network(policy, masked)}")


@main.command(name="list")
@click.option(
    "--for",
    "for_dir",
    metavar="DIR",
    type=_DIR,
    default=None,
    help="show only the containers for DIR",
)
@click.pass_obj
def list_(conn: lxd.Lxd, for_dir: str | None) -> None:
    """List aiab containers with their source dir and mounts."""
    states = conn.instances()
    if for_dir:
        target = _realdir(for_dir)
        wanted = {
            conn.container_for_dir(target, prefix).name
            for agent in agents.AGENT_NAMES
            for prefix in profiles.session_prefixes(agent)
        }
        names = [n for n in states if n in wanted]
    else:
        # Skip the template containers (default and alternate-release bases);
        # they hold no project mounts.
        names = [
            n
            for n in states
            if not release.is_base_container_name(n, agents.AGENT_NAMES)
        ]

    if not names:
        where = f" for {_realdir(for_dir)}" if for_dir else ""
        print(f"No aiab containers{where}.", file=sys.stderr)
        return

    for name in sorted(names):
        _print_container(conn, name, states[name])


# --------------------------------------------------------------------------
# gc
# --------------------------------------------------------------------------

# How each state.prune_stale() record kind reads in the "Pruned ... for DIR"
# line below.
_PRUNE_LABELS = {
    "mounts": "mount record",
    "network": "network record",
    "base": "base record",
    "state": "state dir",
    "limits": "limits record",
    "env": "env record",
}


@main.command()
@click.pass_obj
def gc(conn: lxd.Lxd) -> None:
    """Remove session containers whose source directories no longer exist.

    Also prunes dead entries from the recorded mounts and network policy, and
    the per-directory state dirs (with their saved setup scripts).
    """
    states = conn.instances()
    session_names = [
        n for n in states if not release.is_base_container_name(n, agents.AGENT_NAMES)
    ]

    removed = 0
    for name in sorted(session_names):
        container = conn.container(name)
        devices = container.devices()
        source_dev = None
        for dev_name, dev in devices.items():
            if dev.get("type") == "disk" and lxd.is_source_device(dev_name, name):
                source_dev = dev
                break

        if source_dev is None:
            continue

        source_dir = source_dev.get("source", "")
        if source_dir and Path(source_dir).is_dir():
            continue

        label = source_dir or "(unknown)"
        print(
            f"Removing stale container '{name}' (source: {label}) ...",
            file=sys.stderr,
        )
        _destroy_session(container)
        removed += 1
        print(f"Removed '{name}'.", file=sys.stderr)

    if removed == 0 and not states:
        print("No aiab containers found.", file=sys.stderr)
    elif removed == 0:
        print("No stale containers found.", file=sys.stderr)
    else:
        print(
            f"Removed {removed} stale container{'s' if removed != 1 else ''}.",
            file=sys.stderr,
        )

    for kind, dirs in state.prune_stale().items():
        for d in dirs:
            print(f"Pruned {_PRUNE_LABELS[kind]} for {d}", file=sys.stderr)


# --------------------------------------------------------------------------
# lxc passthrough
# --------------------------------------------------------------------------


@main.command(
    context_settings=dict(ignore_unknown_options=True, allow_interspersed_args=False),
    add_help_option=False,
)
@click.argument("rest", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
def lxc(conn: lxd.Lxd, rest: tuple[str, ...]) -> None:
    """Run lxc against the 'aiab' project (e.g. aiab lxc list)."""
    lxd.ensure_project(conn.project)
    result = subprocess.run(conn.argv(list(rest)))
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
