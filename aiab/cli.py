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
# A Click group with a command per verb (run, remove, mount, unmount,
# upgrade-templates, list, lxc). The LXD engine (Lxd/Container) lives in
# aiab.lxd, provisioning in aiab.provision, and the per-agent data in
# aiab.agents; this module just parses arguments and orchestrates. The Lxd
# connection is built once per invocation and passed to commands via ctx.obj.

from __future__ import annotations

import contextlib
import fcntl
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click

from . import PROJECT, CONTAINER_USER, CONTAINER_HOME, WORK_PREFIX
from . import agents
from . import lxd
from . import provision
from . import state
from .migrate import maybe_migrate

CONFIG_CONTAINER_PATH: str = CONTAINER_HOME  # agent home dir is mounted here

AGENT_CHOICE = click.Choice(agents.AGENT_NAMES)

# Per-container lock files live here. Each 'aiab run' holds a shared flock
# for the duration of its session; the last one to exit stops the container.
_LOCK_DIR: Path = Path.home() / ".local" / "share" / "aiab" / "locks"


def _realdir(path: str | None) -> Path:
    return Path(path).resolve() if path else Path.cwd()


@contextlib.contextmanager
def _auto_stop_on_exit(session: lxd.Container) -> Iterator[None]:
    """Stop the session container on exit if no other aiab process is using it.

    Each 'aiab run' holds a shared flock on a per-container lock file for the
    duration of its session (agent or shell). On exit we try to upgrade to an
    exclusive lock (non-blocking). If that succeeds we are the last process for
    this container and stop it; if it fails another aiab process is still
    running here and we leave the container up.

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
                if session.status() == "RUNNING":
                    print(
                        f"Stopping container '{session.name}' ...",
                        file=sys.stderr,
                    )
                    session.stop()
            except OSError:
                pass  # another aiab process is still using this container


# -- git worktree helpers --

# Worktrees are stored inside the mounted repo's .git directory so they need no
# extra bind-mounts and are invisible to normal directory listings.
_WORKTREE_DIR = ".git/aiab-worktrees"


def _setup_worktree(
    session: lxd.Container, repo_cwd: str, user: int
) -> str:
    """Create a git worktree inside the container; return its path.

    The worktree is a detached HEAD at the current commit, stored under
    <repo>/.git/aiab-worktrees/<session-id>/. A detached HEAD avoids
    creating throwaway branch refs that litter the reflog.
    """
    session_id = str(int(time.time()))
    worktree_path = f"{repo_cwd}/{_WORKTREE_DIR}/{session_id}"

    # Verify it's actually a git repo.
    r = subprocess.run(
        session._argv([
            "exec", session.name,
            f"--cwd={repo_cwd}", f"--user={user}", f"--group={user}",
            "--", "git", "rev-parse", "--git-dir",
        ]),
        capture_output=True,
    )
    if r.returncode != 0:
        raise click.ClickException(
            f"--worktree requires a git repository, but {repo_cwd} is not one"
        )

    # Create the worktree (detached HEAD at current commit).
    session.exec([
        "runuser", "-u", "ubuntu", "--",
        "git", "-C", repo_cwd, "worktree", "add", "--detach", worktree_path,
    ])
    print(f"Created worktree at container:{worktree_path}", file=sys.stderr)
    return worktree_path


def _remove_worktree(
    session: lxd.Container, repo_cwd: str, worktree_path: str, user: int
) -> None:
    """Remove a worktree created by _setup_worktree."""
    try:
        session.exec([
            "runuser", "-u", "ubuntu", "--",
            "git", "-C", repo_cwd, "worktree", "remove", "--force",
            worktree_path,
        ])
        print(f"Removed worktree at container:{worktree_path}", file=sys.stderr)
    except subprocess.CalledProcessError:
        print(
            f"Warning: could not remove worktree {worktree_path}",
            file=sys.stderr,
        )


def _prune_worktrees(session: lxd.Container, repo_cwd: str, user: int) -> None:
    """Prune stale worktree bookkeeping for a repo (e.g. after a crash)."""
    subprocess.run(
        session._argv([
            "exec", session.name,
            f"--cwd={repo_cwd}", f"--user={user}", f"--group={user}",
            "--", "git", "worktree", "prune",
        ]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class _Command(click.Command):
    """A Command that prepares the LXD connection before invoking its body.

    Command.invoke() runs only after a successful parse, so `aiab ... --help`
    (which exits during parsing) triggers neither migration nor project
    creation. Migration must run before ensure_project() — it keys off whether
    the 'aiab' project exists yet, and ensure_project() would create it.
    """

    def invoke(self, ctx: click.Context) -> Any:
        maybe_migrate()
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
    "--shell",
    is_flag=True,
    help="open an interactive shell instead of running the agent",
)
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
def run(
    conn: lxd.Lxd,
    agent: str,
    for_dir: str | None,
    add_mount: tuple[str, ...],
    add_mount_rw: tuple[str, ...],
    worktree: bool,
    worktree_keep: bool,
    shell: bool,
    agent_args: tuple[str, ...],
) -> None:
    """Run an agent in a container for a directory.

    Anything after `--` is passed straight through to the agent.
    """
    # --worktree-keep implies --worktree.
    if worktree_keep:
        worktree = True
    cfg = agents.get(agent)
    config_host_dir = lxd.agent_home_dir(agent)

    # One-time prepare hook (OpenRouter key prompt, opencode permissive config).
    if cfg.prepare:
        cfg.prepare(config_host_dir)

    work_dir = _realdir(for_dir)
    base = conn.container(agent)
    session = conn.container_for_dir(work_dir, agent)

    if not base.exists():
        provision.provision_base(
            base,
            config_host_dir=config_host_dir,
            config_container_path=CONFIG_CONTAINER_PATH,
            config_device_name=f"{agent}config",
            install_cmds=cfg.install_cmds,
            container_user=CONTAINER_USER,
        )
    session.ensure_started(base)

    if shell:
        run_cmd = ["bash", "-l"]
    else:
        cmd_args = list(agent_args)
        if cfg.skip_permissions:
            cmd_args = ["--dangerously-skip-permissions"] + cmd_args
        run_cmd = [cfg.command] + cmd_args

    container_cwd = session.add_device(work_dir, work_prefix=WORK_PREFIX)

    # Record any run-time --add-mount mounts for this directory, then (re)apply
    # every mount recorded for it — so a freshly created or cloned container,
    # and other agents started here later, all pick up the same set.
    for d in add_mount:
        state.set_mount(work_dir, d, readonly=True)
    for d in add_mount_rw:
        state.set_mount(work_dir, d, readonly=False)
    _apply_recorded_mounts(session, work_dir)

    for host_path, overlay_path in cfg.overlays:
        if host_path.exists():
            session.add_config_overlay(
                host_path, overlay_path, container_user=CONTAINER_USER
            )

    # If --worktree was requested, create one inside the repo and use it as
    # the agent's working directory instead of the repo root.
    agent_cwd = container_cwd
    if worktree:
        agent_cwd = _setup_worktree(session, container_cwd, CONTAINER_USER)

    env = {"HOME": CONTAINER_HOME}
    if cfg.wayland:
        env.update(session.mount_wayland(CONTAINER_USER))
    with _auto_stop_on_exit(session):
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
        session.stop()
    print(f"Removing container '{session.name}' ...", file=sys.stderr)
    session.delete()
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


def _apply_recorded_mounts(container: lxd.Container, for_dir: Path) -> None:
    """Add every mount recorded for for_dir to a container.

    Skips (with a warning) any whose source no longer exists, so a recreated
    container still comes up.
    """
    for m in state.get_mounts(for_dir):
        if not Path(m["source"]).is_dir():
            print(
                f"Warning: recorded mount {m['source']} not found; skipping",
                file=sys.stderr,
            )
            continue
        container.add_device(
            m["source"], work_prefix=WORK_PREFIX, readonly=m["readonly"]
        )


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
            container.add_device(path, work_prefix=WORK_PREFIX, readonly=readonly)
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
# upgrade-templates
# --------------------------------------------------------------------------


@main.command("upgrade-templates")
@click.argument("which", nargs=-1, type=AGENT_CHOICE, metavar="[AGENT]...")
@click.pass_obj
def upgrade_templates(conn: lxd.Lxd, which: tuple[str, ...]) -> None:
    """apt upgrade + reinstall the agent in template containers.

    With no arguments, updates all template containers that currently exist.
    """
    targets = which or agents.AGENT_NAMES
    updated = skipped = 0
    for agent in targets:
        cfg = agents.get(agent)
        print(f"=== {agent} ===", file=sys.stderr)
        ok = provision.update_template(
            conn.container(agent),
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


def _print_container(conn: lxd.Lxd, name: str, status: str) -> None:
    devices = conn.container(name).devices()
    source = None
    extras = []
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
        # Skip the bare base/template containers (named exactly after an agent);
        # they hold no project mounts.
        names = [n for n in states if n not in agents.AGENT_NAMES]

    if not names:
        where = f" for {_realdir(for_dir)}" if for_dir else ""
        print(f"No aiab containers{where}.", file=sys.stderr)
        return

    for name in sorted(names):
        _print_container(conn, name, states[name])


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
