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
# aiab.lxd, provisioning in aiab.provision, and the per-agent data in
# aiab.agents; this module just parses arguments and orchestrates. The Lxd
# connection is built once per invocation and passed to commands via ctx.obj.

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import tempfile
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click

from . import PROJECT, CONTAINER_USER, CONTAINER_HOME, WORK_PREFIX, STATE_MOUNT
from . import agents
from . import lxd
from . import netproxy
from . import netwatch
from . import provision
from . import release
from . import state

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

# Per-container lock files live here. Each 'aiab run' holds a shared flock
# for the duration of its session; when the last one exits, a detached helper
# (aiab.stopper) stops the container after IDLE_STOP_DELAY unless a new
# session has taken the lock again — so exiting doesn't block on the stop,
# and back-to-back sessions reuse the still-running container.
_LOCK_DIR: Path = Path.home() / ".local" / "share" / "aiab" / "locks"

# How long a container stays up after its last session exits (seconds).
IDLE_STOP_DELAY: float = 5 * 60

# The stopper helpers log here, named after the container.
_STOPPER_DIR: Path = Path.home() / ".local" / "share" / "aiab" / "stopper"

# Host-side filtering proxies (one per restricted session container, see
# aiab.netproxy) keep their pidfile/log in netproxy.PROXY_DIR, named after the
# container. The proxy itself listens on an abstract unix socket (no
# filesystem presence): snap-confined LXD's forkproxy can't dial socket paths
# under the user's home, but abstract sockets live in the (shared) network
# namespace.
_PROXY_DIR: Path = netproxy.PROXY_DIR

# Domains every restricted container may always reach, whatever the agent:
# the Ubuntu archives, so apt works inside the container (apt's traffic is
# proxy-aware plain HTTP, which the proxy forwards).
BASELINE_DOMAINS: list[str] = ["archive.ubuntu.com", "security.ubuntu.com"]


def _proxy_socket_name(container_name: str) -> str:
    """The abstract socket address for a container's proxy, with leading @.

    Understood in this form by both aiab.netproxy (--socket) and LXD proxy
    devices (connect=unix:@...). Includes the uid so concurrent aiab users on
    one host can't collide in the abstract namespace.
    """
    return f"@aiab-{os.getuid()}-{container_name}"


def _agent_command(
    cfg: agents.Agent, agent_args: tuple[str, ...], shell: bool
) -> list[str]:
    """Build the command run interactively inside the session container."""
    if shell:
        return ["bash", "-l"]
    cmd_args = list(cfg.extra_args) + list(agent_args)
    return [cfg.command] + cmd_args


def _realdir(path: str | None) -> Path:
    return Path(path).resolve() if path else Path.cwd()


def _helper_env() -> dict[str, str]:
    """Env for detached helper processes (netproxy, stopper).

    They run `python -m aiab.<module>`, so the aiab package must be
    importable regardless of cwd.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [str(agents.REPO_ROOT), env.get("PYTHONPATH")] if p
    )
    return env


# -- filtering proxy lifecycle --


def _proxy_pid(container_name: str) -> int | None:
    """Return the pid of a live proxy for a container, or None."""
    try:
        pid = int((_PROXY_DIR / f"{container_name}.pid").read_text())
        os.kill(pid, 0)  # just probes for existence
    except (OSError, ValueError):
        return None
    return pid


def _proxy_socket_live(socket_name: str) -> bool:
    """Return True if something accepts connections on an @abstract socket."""
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect("\0" + socket_name[1:])
    except OSError:
        return False
    finally:
        s.close()
    return True


def _ensure_proxy(
    session: lxd.Container, work_dir: Path, api_domains: list[str]
) -> str:
    """Start the filtering proxy for a session container (or reuse a live one).

    Returns the abstract socket address (with leading @) the proxy listens
    on. The proxy is shared by concurrent `aiab run`s for the same container
    and stopped alongside the container by _auto_stop_on_exit.
    """
    _PROXY_DIR.mkdir(parents=True, exist_ok=True)
    sock_name = _proxy_socket_name(session.name)
    log = _PROXY_DIR / f"{session.name}.log"
    if _proxy_pid(session.name) is not None and _proxy_socket_live(sock_name):
        return sock_name

    argv = [
        sys.executable,
        "-m",
        "aiab.netproxy",
        f"--socket={sock_name}",
        f"--dir={work_dir}",
        f"--pending-dir={netwatch.pending_dir(work_dir)}",
    ] + [f"--api-domain={d}" for d in api_domains + BASELINE_DOMAINS]
    with log.open("ab") as log_fd:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            env=_helper_env(),
        )
    (_PROXY_DIR / f"{session.name}.pid").write_text(f"{proc.pid}\n")

    # Wait for the socket so the LXD proxy device has something to connect to.
    for _ in range(50):
        if _proxy_socket_live(sock_name):
            break
        if proc.poll() is not None:
            raise click.ClickException(f"network proxy failed to start; see {log}")
        time.sleep(0.1)
    else:
        raise click.ClickException(f"network proxy did not come up; see {log}")
    print(f"Started filtering proxy (denials logged to {log})", file=sys.stderr)
    return sock_name


def _stop_proxy(container_name: str) -> None:
    """Stop the proxy for a container, if one is running, and clean up.

    The abstract socket disappears with the process; the .sock unlink only
    cleans up files left by versions that used filesystem sockets.
    """
    pid = _proxy_pid(container_name)
    if pid is not None:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    (_PROXY_DIR / f"{container_name}.pid").unlink(missing_ok=True)
    (_PROXY_DIR / f"{container_name}.sock").unlink(missing_ok=True)


def _spawn_stopper(container_name: str) -> None:
    """Launch the detached helper that stops an idle container later."""
    _STOPPER_DIR.mkdir(parents=True, exist_ok=True)
    log = _STOPPER_DIR / f"{container_name}.log"
    with log.open("ab") as log_fd:
        subprocess.Popen(
            [sys.executable, "-m", "aiab.stopper", container_name],
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            env=_helper_env(),
        )


@contextlib.contextmanager
def _stop_when_idle(session: lxd.Container) -> Iterator[None]:
    """Hold the session lock; on exit, schedule a stop if we were the last.

    Each 'aiab run' holds a shared flock on a per-container lock file for the
    duration of its session (agent or shell). The lock is taken *before* the
    container is started, so a pending stopper can never shoot down a
    container that a new run has just brought up. On exit we try to upgrade
    to an exclusive lock (non-blocking): if that fails another aiab process
    is still using the container; if it succeeds we are the last one out and
    spawn aiab.stopper, which stops the container IDLE_STOP_DELAY seconds
    later unless a new session has taken the lock by then. The proxy teardown
    moves to the stopper too, so a quick follow-up session can reuse a live
    proxy.

    The OS releases flocks automatically on process death, so there are no
    stale lock files to handle after a crash.
    """
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with (_LOCK_DIR / session.name).open("w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        try:
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass  # another aiab process is still using this container
            else:
                _spawn_stopper(session.name)
                print(
                    f"Container '{session.name}' stops in "
                    f"{int(IDLE_STOP_DELAY // 60)} minutes unless a new "
                    "session starts.",
                    file=sys.stderr,
                )


# -- git worktree helpers --

# Worktrees are stored inside the mounted repo's .git directory so they need no
# extra bind-mounts and are invisible to normal directory listings.
_WORKTREE_DIR = ".git/aiab-worktrees"


def _setup_worktree(session: lxd.Container, repo_cwd: str, user: int) -> str:
    """Create a git worktree inside the container; return its path.

    The worktree is a detached HEAD at the current commit, stored under
    <repo>/.git/aiab-worktrees/<session-id>/. A detached HEAD avoids
    creating throwaway branch refs that litter the reflog.
    """
    session_id = str(int(time.time()))
    worktree_path = f"{repo_cwd}/{_WORKTREE_DIR}/{session_id}"

    # Verify it's actually a git repo.
    r = subprocess.run(
        session._argv(
            [
                "exec",
                session.name,
                f"--cwd={repo_cwd}",
                f"--user={user}",
                f"--group={user}",
                "--",
                "git",
                "rev-parse",
                "--git-dir",
            ]
        ),
        capture_output=True,
    )
    if r.returncode != 0:
        raise click.ClickException(
            f"--worktree requires a git repository, but {repo_cwd} is not one"
        )

    # Create the worktree (detached HEAD at current commit).
    session.exec(
        [
            "runuser",
            "-u",
            "ubuntu",
            "--",
            "git",
            "-C",
            repo_cwd,
            "worktree",
            "add",
            "--detach",
            worktree_path,
        ]
    )
    print(f"Created worktree at container:{worktree_path}", file=sys.stderr)
    return worktree_path


def _remove_worktree(
    session: lxd.Container, repo_cwd: str, worktree_path: str, user: int
) -> None:
    """Remove a worktree created by _setup_worktree."""
    try:
        session.exec(
            [
                "runuser",
                "-u",
                "ubuntu",
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


def _prune_worktrees(session: lxd.Container, repo_cwd: str, user: int) -> None:
    """Prune stale worktree bookkeeping for a repo (e.g. after a crash)."""
    subprocess.run(
        session._argv(
            [
                "exec",
                session.name,
                f"--cwd={repo_cwd}",
                f"--user={user}",
                f"--group={user}",
                "--",
                "git",
                "worktree",
                "prune",
            ]
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
# The monitor's presence is also what switches the proxy from fail-fast 403s
# to parking unknown hosts for an interactive decision.


def _self_argv0() -> str:
    """An absolute path for re-invoking aiab (sys.argv[0] may be a bare name)."""
    argv0 = sys.argv[0]
    if os.sep in argv0:
        return str(Path(argv0).resolve())
    return shutil.which(argv0) or argv0


def _reexec_under_tmux() -> None:
    """Run this aiab invocation inside a new tmux session, then return.

    Stderr from the inner process is teed to a temp file while still showing
    live in the pane.  After tmux closes, if the inner exit code was non-zero
    and the log has content, the log is printed to the outer terminal so that
    errors do not disappear with the pane.

    The trailing set-option turns mouse mode on for this session only (no -g,
    so a user's own tmux sessions and config are untouched): clicks then
    switch pane focus, and reach the watch pane's allow/deny buttons even
    while the agent pane has focus.
    """
    inner = shlex.join([_self_argv0(), *sys.argv[1:]])

    # Pre-create the log and rc files so we know the paths before launching.
    log_fd, log_path = tempfile.mkstemp(prefix="aiab-stderr-", suffix=".log")
    os.close(log_fd)
    rc_fd, rc_path = tempfile.mkstemp(prefix="aiab-rc-", suffix=".txt")
    os.close(rc_fd)

    # Write a bash wrapper that tees stderr and records the exit code, both to
    # files the outer process can read after tmux exits.
    script_fd, script_path = tempfile.mkstemp(prefix="aiab-run-", suffix=".sh")
    try:
        os.write(
            script_fd,
            (
                "#!/bin/bash\n"
                f"{inner} 2> >(tee {shlex.quote(log_path)} >&2)\n"
                f"echo $? > {shlex.quote(rc_path)}\n"
            ).encode(),
        )
    finally:
        os.close(script_fd)
    os.chmod(script_path, 0o700)

    subprocess.run(
        [
            "tmux",
            "new-session",
            "-c",
            os.getcwd(),
            script_path,
            ";",
            "set-option",
            "mouse",
            "on",
        ]
    )
    os.unlink(script_path)

    # Read the inner process's exit code (tmux does not propagate it).
    inner_rc = 1
    try:
        inner_rc = int(Path(rc_path).read_text().strip())
    except (OSError, ValueError):
        pass
    finally:
        Path(rc_path).unlink(missing_ok=True)

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

    Command.invoke() runs only after a successful parse, so `aiab ... --help`
    (which exits during parsing) does not create the project.

    Auto-migration from the old lxd-* layout (aiab.migrate) is intentionally
    not wired in here for now; ensure_project() just creates the 'aiab' project
    on first use.
    """

    def invoke(self, ctx: click.Context) -> Any:
        conn = lxd.Lxd(PROJECT)
        conn.ensure_project()
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
    conn: lxd.Lxd, session: lxd.Container, work_dir: Path, cfg: agents.Agent
) -> dict[str, str]:
    """Apply the recorded network policy and return proxy env vars."""
    policy = state.get_network(work_dir)
    nic_names = conn.profile_nic_names()
    if policy["mode"] != state.MODE_RESTRICTED:
        session.unmask_profile_devices(nic_names)
        session.remove_device("netproxy")
        return {}

    session.mask_profile_devices(nic_names)
    sock_name = _ensure_proxy(session, work_dir, cfg.api_domains)
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
    session: lxd.Container, cfg: agents.Agent, proxy_env: dict[str, str]
) -> dict[str, str]:
    """Environment for the agent or shell process inside the container."""
    env = {"HOME": CONTAINER_HOME, "PATH": _CONTAINER_PATH}
    env.update(proxy_env)
    if cfg.wayland:
        env.update(session.mount_wayland(CONTAINER_USER))
    return env


@main.command()
@click.argument("agent", type=AGENT_CHOICE)
@click.option(
    "--for",
    "for_dir",
    metavar="DIR",
    default=None,
    help="run the agent for DIR (default: current directory)",
)
@click.option(
    "--add-mount",
    "add_mount",
    metavar="DIR",
    multiple=True,
    help="mount DIR read-only and record it for this directory (repeatable)",
)
@click.option(
    "--add-mount-rw",
    "add_mount_rw",
    metavar="DIR",
    multiple=True,
    help="mount DIR read-write and record it for this directory (repeatable)",
)
@click.option(
    "--base",
    "base_release",
    metavar="RELEASE",
    default=None,
    help="build/use Ubuntu RELEASE (e.g. 22.04 or jammy) and record it for "
    "this directory",
)
@click.option(
    "--worktree",
    is_flag=True,
    help="run the agent in a fresh git worktree (branched from HEAD)",
)
@click.option(
    "--worktree-keep",
    is_flag=True,
    help="keep the worktree after the agent exits (implies --worktree)",
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
    worktree: bool,
    worktree_keep: bool,
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
    # --worktree-keep implies --worktree.
    if worktree_keep:
        worktree = True
    cfg = agents.get(agent)
    config_host_dir = lxd.agent_home_dir(agent)

    work_dir = _realdir(for_dir)

    # --base records the directory's Ubuntu release (like --add-mount records a
    # mount), then uses it below; without it we use the directory's recorded
    # base, or the default.
    if base_release:
        try:
            state.set_base(work_dir, release.normalize(base_release))
        except ValueError as e:
            sys.exit(f"Error: {e}")
    dir_base = state.get_base(work_dir)

    # Wrap sessions in tmux (see the tmux control plane section).
    # Inside tmux already — ours or the user's — _watch_pane below splits the
    # current window instead, so this re-exec only fires on a bare terminal.
    use_tmux = (
        not no_tmux
        and sys.stdin.isatty()
        and shutil.which("tmux") is not None
    )
    if use_tmux and "TMUX" not in os.environ:
        _reexec_under_tmux()  # does not return

    # One-time prepare hook (OpenRouter key prompt, opencode permissive config).
    if cfg.prepare:
        cfg.prepare(config_host_dir)

    base = conn.container(release.base_container_name(agent, dir_base))
    session = conn.container_for_dir(work_dir, agent)

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
            config_host_dir=config_host_dir,
            config_container_path=CONFIG_CONTAINER_PATH,
            config_device_name=f"{agent}config",
            install_cmds=cfg.install_cmds,
            container_user=CONTAINER_USER,
        )
    # The session lock is held from before the container starts, so a pending
    # stopper from an earlier session can't stop it out from under us mid-setup.
    with _stop_when_idle(session):
        session.ensure_started(base)
        # Record the base this session was cloned from, so a later base change
        # for the directory is detected and triggers a rebuild (above).
        session.set_config("user.aiab_base", dir_base)
        session.apply_limits(**state.get_limits(work_dir))
        provision.apply_session_tweaks(session)

        run_cmd = _agent_command(cfg, agent_args, shell)
        container_cwd, applied_mounts = _apply_session_mounts(
            session, cfg, work_dir, add_mount, add_mount_rw
        )
        proxy_env = _apply_network_policy(conn, session, work_dir, cfg)

        # If --worktree was requested, create one inside the repo and use it
        # as the agent's working directory instead of the repo root.
        agent_cwd = container_cwd
        if worktree:
            agent_cwd = _setup_worktree(session, container_cwd, CONTAINER_USER)

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

        env = _session_env(session, cfg, proxy_env)
        with _monitor_pane(work_dir, session.name, enabled=use_tmux):
            rc = session.run_interactive(
                run_cmd,
                cwd=agent_cwd,
                user=CONTAINER_USER,
                group=CONTAINER_USER,
                env=env,
            )
        if worktree and not worktree_keep:
            _remove_worktree(session, container_cwd, agent_cwd, CONTAINER_USER)
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
        _stop_proxy(session.name)
        session.remove_device("netproxy")
        session.stop(timeout=30)
    session.delete()
    _stop_proxy(session.name)


@main.command()
@click.argument("agent", type=AGENT_CHOICE)
@click.option(
    "--for",
    "for_dir",
    metavar="DIR",
    default=None,
    help="target the container for DIR (default: current directory)",
)
@click.pass_obj
def remove(conn: lxd.Lxd, agent: str, for_dir: str | None) -> None:
    """Delete the session container for a directory.

    The base/template container is left intact, so the next run clones a fresh
    one quickly. Any leftover git worktrees created by --worktree are pruned
    from the host directory before deleting the container.
    """
    work_dir = _realdir(for_dir)
    session = conn.container_for_dir(work_dir, agent)
    if not session.exists():
        print(f"No container '{session.name}' to remove.", file=sys.stderr)
        return
    # Prune stale worktrees before deleting — only possible if the container is
    # running (exec needs a live container). Start it temporarily if needed.
    was_stopped = session.status() != "RUNNING"
    if was_stopped:
        session.start()
    container_cwd = session.add_device(work_dir, work_prefix=WORK_PREFIX)
    _prune_worktrees(session, container_cwd, CONTAINER_USER)
    if was_stopped:
        session.stop(timeout=30)
    print(f"Removing container '{session.name}' ...", file=sys.stderr)
    session.delete()
    _stop_proxy(session.name)  # in case a crashed session left one behind
    print(f"Removed container '{session.name}'.", file=sys.stderr)


# --------------------------------------------------------------------------
# mount / unmount
# --------------------------------------------------------------------------


def _agent_containers(
    conn: lxd.Lxd, for_dir: Path
) -> Iterator[tuple[str, lxd.Container]]:
    """Yield (agent, Container) for every existing agent container of a dir."""
    for agent in agents.AGENT_NAMES:
        container = conn.container_for_dir(for_dir, agent)
        if container.exists():
            yield agent, container


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
    default=None,
    help="target the containers for DIR (default: current directory)",
)
@click.option(
    "--ro/--rw",
    "readonly",
    default=True,
    help="mount read-only (the default) or read-write",
)
@click.argument("dirs", nargs=-1, required=True, metavar="DIR...")
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
    for agent, container in _agent_containers(conn, target):
        found = True
        print(f"=== {agent} ({container.name}) ===", file=sys.stderr)
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
    default=None,
    help="target the containers for DIR (default: current directory)",
)
@click.argument("dirs", nargs=-1, required=True, metavar="DIR...")
@click.pass_obj
def unmount(conn: lxd.Lxd, for_dir: str | None, dirs: tuple[str, ...]) -> None:
    """Remove extra directory mounts from a directory's containers."""
    target = _realdir(for_dir)
    paths = [Path(p).resolve() for p in dirs]

    # Drop from the persistent record so it isn't replayed on the next run.
    for path in paths:
        if not state.remove_mount(target, path):
            print(f"Not recorded: {path}", file=sys.stderr)

    for agent, container in _agent_containers(conn, target):
        print(f"=== {agent} ({container.name}) ===", file=sys.stderr)
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


# A plain Group: the net commands only edit recorded state, so they skip the
# _Command machinery (migration check + LXD connection) the other verbs need.
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
    default=None,
    help="target DIR (default: current directory)",
)


@net.command()
@_for_dir_option
def status(for_dir: str | None) -> None:
    """Show the network mode and allow/deny lists for a directory."""
    target = _realdir(for_dir)
    policy = state.get_network(target)
    print(f"{target}: {policy['mode']}")
    if policy["mode"] != state.MODE_RESTRICTED:
        return
    print("always allowed:")
    print(f"  baseline: {', '.join(BASELINE_DOMAINS)}")
    for name in agents.AGENT_NAMES:
        domains = agents.get(name).api_domains
        print(f"  {name}: {', '.join(domains) if domains else '(none)'}")
    if policy["allow"]:
        print("allowed domains:")
        for a in policy["allow"]:
            print(f"  {a['domain']}{_format_expiry(a['expires'])}")
    else:
        print("allowed domains: (none)")
    if policy["deny"]:
        print("denied domains:")
        for d in policy["deny"]:
            print(f"  {d}")


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
@click.argument("domains", nargs=-1, required=True, metavar="DOMAIN...")
def allow(for_dir: str | None, duration: str | None, domains: tuple[str, ...]) -> None:
    """Allow domains (and their subdomains) for a directory.

    Takes effect immediately in running restricted sessions. Re-allowing a
    domain replaces its expiry, so a plain `allow` makes a temporary grant
    permanent.
    """
    target = _realdir(for_dir)
    expires = time.time() + _parse_duration(duration) if duration else None
    for domain in domains:
        state.add_network_allow(target, domain, expires)
        print(f"Allowed {domain}{_format_expiry(expires)}", file=sys.stderr)
    if state.get_network(target)["mode"] != state.MODE_RESTRICTED:
        print(
            "Note: network mode here is open; the allowlist only takes "
            "effect after 'aiab net restrict'.",
            file=sys.stderr,
        )


@net.command()
@_for_dir_option
@click.argument("domains", nargs=-1, required=True, metavar="DOMAIN...")
def deny(for_dir: str | None, domains: tuple[str, ...]) -> None:
    """Deny domains (and their subdomains) for a directory.

    Drops the domains from the allowlist and records them on the denylist,
    so requests fail fast instead of prompting a watch session. Takes effect
    immediately in running restricted sessions; 'aiab net allow' reverses
    it. The agent's own API domains cannot be denied.
    """
    target = _realdir(for_dir)
    for domain in domains:
        state.add_network_deny(target, domain)
        print(f"Denied {domain}", file=sys.stderr)


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
    Given a RELEASE (a version like 22.04 or a codename like jammy), records it
    for the directory; 'default' clears it back to the built-in default. A
    change takes effect the next time an agent starts here — its container is
    rebuilt from the new base then.
    """
    target = _realdir(for_dir)

    if release_arg is None:
        current = state.get_base(target)
        print(f"{target}: {current}")
        if current != release.DEFAULT_BASE:
            print(f"default: {release.DEFAULT_BASE}")
        known = ", ".join(
            f"{v} ({c})"
            for c, v in sorted(release.CODENAMES.items(), key=lambda kv: kv[1])
        )
        print(f"known releases: {known}")
        return

    if release_arg.strip().lower() == "default":
        canonical = release.DEFAULT_BASE
    else:
        try:
            canonical = release.normalize(release_arg)
        except ValueError as e:
            sys.exit(f"Error: {e}")
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

_SIZE_RE = re.compile(
    r"^\d+(\.\d+)?\s*(KiB|MiB|GiB|TiB|KB|MB|GB|TB)$", re.IGNORECASE
)


def _validate_size(value: str, label: str) -> str:
    if not _SIZE_RE.match(value.strip()):
        raise click.BadParameter(
            f"invalid {label} {value!r} — expected e.g. 8GiB or 512MiB"
        )
    return value.strip()


@main.command(cls=click.Command)
@_for_dir_option
@click.option("--cpu", "cpu", metavar="N", default=None, type=int,
              help="number of vCPUs")
@click.option("--memory", "memory", metavar="SIZE", default=None,
              help="memory limit, e.g. 8GiB")
@click.option("--reset", "reset", is_flag=True,
              help="reset all limits to defaults")
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

    if cpu is not None and cpu < 1:
        raise click.BadParameter("cpu must be at least 1")

    if memory is not None:
        memory = _validate_size(memory, "memory")

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


def _launch_monitor(target: Path, container_name: str | None, plain: bool) -> None:
    """Open the session control panel for a directory (never returns).

    Prefers the textual UI (aiab.monitor_tui: clickable network decisions plus
    a mounts view); falls back to the plain keystroke network console when
    textual is missing or --plain is given. container_name names the session
    container a mounts edit should touch live, when `aiab run` opened the pane.
    """
    if not plain:
        try:
            from . import monitor_tui
        except ImportError:
            pass  # textual missing or too old (e.g. Ubuntu's 0.1.x package)
        else:
            sys.exit(monitor_tui.monitor(target, container_name))
    sys.exit(netwatch.watch(target))


# --container is set by the `aiab run` tmux pane (see _monitor_pane); it names
# the session container whose live mounts the mounts view should edit. Hidden
# because it is plumbing, not something a user types by hand.
_container_option = click.option(
    "--container", "container_name", metavar="NAME", default=None, hidden=True
)


# A plain Command (like the net group): the monitor only reads/writes recorded
# state and drives LXD lazily from the TUI, so it skips the _Command machinery
# (migration check + eager project creation) the container verbs need.
@main.command(cls=click.Command)
@_for_dir_option
@_container_option
@click.option(
    "--plain",
    is_flag=True,
    help="use the plain keystroke console even when textual is available",
)
def monitor(for_dir: str | None, container_name: str | None, plain: bool) -> None:
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

    With textual installed each prompt is a row of clickable buttons; the
    plain keystroke network console is the fallback, and --plain forces it.
    `aiab run` opens this in a tmux pane automatically; it also works
    standalone in any terminal.
    """
    _launch_monitor(_realdir(for_dir), container_name, plain)


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
    devices = conn.container(name).devices()
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
    default=None,
    help="show only the containers for DIR",
)
@click.pass_obj
def list_(conn: lxd.Lxd, for_dir: str | None) -> None:
    """List aiab containers with their source dir and mounts."""
    states = conn.instances()
    if for_dir:
        target = _realdir(for_dir)
        wanted = {conn.container_for_dir(target, a).name for a in agents.AGENT_NAMES}
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
        if states[name] == "RUNNING":
            _stop_proxy(name)
            container.remove_device("netproxy")
            container.stop(timeout=30)
        container.delete()
        _stop_proxy(name)
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

    pruned_mounts, pruned_net, pruned_base, pruned_state, pruned_limits = (
        state.prune_stale()
    )
    for d in pruned_mounts:
        print(f"Pruned mount record for {d}", file=sys.stderr)
    for d in pruned_net:
        print(f"Pruned network record for {d}", file=sys.stderr)
    for d in pruned_base:
        print(f"Pruned base record for {d}", file=sys.stderr)
    for d in pruned_state:
        print(f"Pruned state dir for {d}", file=sys.stderr)
    for d in pruned_limits:
        print(f"Pruned limits record for {d}", file=sys.stderr)


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
    result = subprocess.run(conn.argv(list(rest)))
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
