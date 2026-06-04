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
# upgrade-templates, list, lxc). The engine lives in aiab.lxd and the per-agent
# data in aiab.agents; this module just parses arguments and orchestrates.

import subprocess
import sys
from pathlib import Path

import click

from . import PROJECT, CONTAINER_USER, CONTAINER_HOME, WORK_PREFIX
from . import agents
from . import lxd
from . import state
from .migrate import maybe_migrate

CONFIG_CONTAINER_PATH = CONTAINER_HOME  # agent home dir is mounted here

AGENT_CHOICE = click.Choice(agents.AGENT_NAMES)


def _realdir(path):
    return Path(path).resolve() if path else Path.cwd()


def _setup():
    """Per-invocation setup, run before each command body (see _Command).

    Migrate from the old lxd-* layout first, if present: maybe_migrate() keys
    off whether the 'aiab' project exists yet, so it must run before
    use_project() (which would create that project and defeat the trigger).
    """
    maybe_migrate()
    lxd.use_project(PROJECT)


class _Command(click.Command):
    """A Command that runs _setup() before invoking its body.

    Command.invoke() runs only after a successful parse, so `aiab ... --help`
    (which exits during parsing) never triggers migration or project creation.
    """
    def invoke(self, ctx):
        _setup()
        return super().invoke(ctx)


class _Group(click.Group):
    command_class = _Command


@click.group(cls=_Group)
def main():
    """Run coding agents in disposable per-directory LXD containers."""


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

@main.command()
@click.argument("agent", type=AGENT_CHOICE)
@click.option("--for", "for_dir", metavar="DIR", default=None,
              help="run the agent for DIR (default: current directory)")
@click.option("--also", "also", metavar="DIR", multiple=True,
              help="also mount DIR (read-only) into the container (repeatable)")
@click.option("--also-rw", "also_rw", metavar="DIR", multiple=True,
              help="also mount DIR (read-write) into the container (repeatable)")
@click.option("--shell", is_flag=True,
              help="open an interactive shell instead of running the agent")
@click.argument("agent_args", nargs=-1, type=click.UNPROCESSED)
def run(agent, for_dir, also, also_rw, shell, agent_args):
    """Run an agent in a container for a directory.

    Anything after `--` is passed straight through to the agent.
    """
    cfg = agents.get(agent)
    config_host_dir = lxd.agent_home_dir(agent)

    # One-time prepare hook (OpenRouter key prompt, opencode permissive config).
    if cfg.prepare:
        cfg.prepare(config_host_dir)

    for_dir = _realdir(for_dir)
    session = lxd.container_name_for_dir(for_dir, prefix=agent)

    if not lxd.container_exists(agent):
        lxd.setup_base_container(
            base_container=agent,
            config_host_dir=config_host_dir,
            config_container_path=CONFIG_CONTAINER_PATH,
            config_device_name=f"{agent}config",
            install_cmds=cfg.install_cmds,
            container_user=CONTAINER_USER,
        )
    lxd.ensure_session_container(session, base_container=agent)

    if shell:
        run_cmd = ["bash", "-l"]
    else:
        cmd_args = list(agent_args)
        if cfg.skip_permissions:
            cmd_args = ["--dangerously-skip-permissions"] + cmd_args
        run_cmd = [cfg.command] + cmd_args

    container_cwd = lxd.add_device(session, for_dir, work_prefix=WORK_PREFIX)

    # Record any run-time --also mounts for this directory, then (re)apply
    # every mount recorded for it — so a freshly created or cloned container,
    # and other agents started here later, all pick up the same set.
    for d in also:
        state.set_mount(for_dir, d, readonly=True)
    for d in also_rw:
        state.set_mount(for_dir, d, readonly=False)
    _apply_recorded_mounts(session, for_dir)

    for host_path, overlay_path in cfg.overlays:
        if host_path.exists():
            lxd.add_config_overlay(session, host_path, overlay_path,
                                   container_user=CONTAINER_USER)

    exec_cmd = lxd.lxc_argv(["exec", session, f"--cwd={container_cwd}",
                             f"--user={CONTAINER_USER}",
                             f"--group={CONTAINER_USER}",
                             f"--env=HOME={CONTAINER_HOME}"])
    if cfg.wayland:
        exec_cmd += lxd.add_wayland_socket(session, CONTAINER_USER)
    result = subprocess.run(exec_cmd + ["--"] + run_cmd)
    sys.exit(result.returncode)


# --------------------------------------------------------------------------
# remove
# --------------------------------------------------------------------------

@main.command()
@click.argument("agent", type=AGENT_CHOICE)
@click.option("--for", "for_dir", metavar="DIR", default=None,
              help="target the container for DIR (default: current directory)")
def remove(agent, for_dir):
    """Delete the session container for a directory."""
    target = _realdir(for_dir)
    session = lxd.container_name_for_dir(target, prefix=agent)
    lxd.remove_session_container(session)


# --------------------------------------------------------------------------
# mount / unmount
# --------------------------------------------------------------------------

def _agent_containers(for_dir):
    """Yield (agent, container) for every existing agent container of a dir."""
    for agent in agents.AGENT_NAMES:
        container = lxd.container_name_for_dir(for_dir, prefix=agent)
        if lxd.container_exists(container):
            yield agent, container


def _apply_recorded_mounts(container, for_dir):
    """Add every mount recorded for for_dir to a container.

    Skips (with a warning) any whose source no longer exists, so a recreated
    container still comes up.
    """
    for m in state.get_mounts(for_dir):
        if not Path(m["source"]).is_dir():
            print(f"Warning: recorded mount {m['source']} not found; skipping",
                  file=sys.stderr)
            continue
        lxd.add_device(container, m["source"], work_prefix=WORK_PREFIX,
                       readonly=m["readonly"])


@main.command()
@click.option("--for", "for_dir", metavar="DIR", default=None,
              help="target the containers for DIR (default: current directory)")
@click.option("--ro/--rw", "readonly", default=True,
              help="mount read-only (the default) or read-write")
@click.argument("dirs", nargs=-1, required=True, metavar="DIR...")
def mount(for_dir, readonly, dirs):
    """Mount extra directories into a directory's containers."""
    for_dir = _realdir(for_dir)
    paths = [Path(p).resolve() for p in dirs]

    # Persist for the directory first, so a later run / another agent (and a
    # recreated container) get the same mounts.
    for path in paths:
        state.set_mount(for_dir, path, readonly=readonly)

    found = False
    for agent, container in _agent_containers(for_dir):
        found = True
        print(f"=== {agent} ({container}) ===", file=sys.stderr)
        if lxd.container_status(container) != "RUNNING":
            print("Note: container is not running; mounts apply when it next "
                  "starts.", file=sys.stderr)
        for path in paths:
            lxd.add_device(container, path, work_prefix=WORK_PREFIX,
                           readonly=readonly)
    if not found:
        print("No containers exist for this directory yet; the mounts are "
              "recorded and will apply when you next run an agent here.",
              file=sys.stderr)


@main.command()
@click.option("--for", "for_dir", metavar="DIR", default=None,
              help="target the containers for DIR (default: current directory)")
@click.argument("dirs", nargs=-1, required=True, metavar="DIR...")
def unmount(for_dir, dirs):
    """Remove extra directory mounts from a directory's containers."""
    for_dir = _realdir(for_dir)
    paths = [Path(p).resolve() for p in dirs]

    # Drop from the persistent record so it isn't replayed on the next run.
    for path in paths:
        if not state.remove_mount(for_dir, path):
            print(f"Not recorded: {path}", file=sys.stderr)

    for agent, container in _agent_containers(for_dir):
        print(f"=== {agent} ({container}) ===", file=sys.stderr)
        for path in paths:
            if lxd.remove_dir_device(container, path):
                print(f"Unmounted {path}", file=sys.stderr)
            else:
                print(f"Not mounted: {path}", file=sys.stderr)


# --------------------------------------------------------------------------
# upgrade-templates
# --------------------------------------------------------------------------

@main.command("upgrade-templates")
@click.argument("which", nargs=-1, type=AGENT_CHOICE, metavar="[AGENT]...")
def upgrade_templates(which):
    """apt upgrade + reinstall the agent in template containers.

    With no arguments, updates all template containers that currently exist.
    """
    targets = which or agents.AGENT_NAMES
    updated = skipped = 0
    for agent in targets:
        cfg = agents.get(agent)
        print(f"=== {agent} ===", file=sys.stderr)
        ok = lxd.update_base_container(
            base_container=agent,
            update_cmds=cfg.upgrade_cmds,
            container_user=CONTAINER_USER,
        )
        updated += ok
        skipped += not ok
    print(f"Done: {updated} updated, {skipped} skipped.", file=sys.stderr)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def _container_states():
    """Return a {name: state} map for every instance in the project."""
    result = subprocess.run(
        lxd.lxc_argv(["list", "--format=csv", "--columns=n,s"]),
        capture_output=True, text=True, check=True)
    states = {}
    for line in result.stdout.splitlines():
        if line.strip():
            name, _, state = line.partition(",")
            states[name] = state
    return states


def _fmt_mount(dev):
    line = f"{dev.get('source', '?')} -> {dev.get('path', '?')}"
    if str(dev.get("readonly", "false")).lower() == "true":
        line += " (ro)"
    return line


def _print_container(name, state):
    devices = lxd._get_devices(name)
    # The working-directory mount is the dir-* device whose path hash matches
    # the container name's trailing hash (both derive from the same path; the
    # name uses md5[:6], the device md5[:8]). Everything else dir-* is an extra.
    suffix = name.rsplit("-", 1)[-1]
    source = None
    extras = []
    for dev_name, dev in sorted(devices.items()):
        if dev.get("type") != "disk" or not dev_name.startswith("dir-"):
            continue
        if dev_name[4:10] == suffix:
            source = dev
        else:
            extras.append(dev)

    print(f"{name}  [{state}]")
    print(f"  source: {_fmt_mount(source)}" if source
          else "  source: (none)")
    for dev in extras:
        print(f"  mount:  {_fmt_mount(dev)}")


@main.command(name="list")
@click.option("--for", "for_dir", metavar="DIR", default=None,
              help="show only the containers for DIR")
def list_(for_dir):
    """List aiab containers with their source dir and mounts."""
    states = _container_states()
    if for_dir:
        target = _realdir(for_dir)
        wanted = {lxd.container_name_for_dir(target, prefix=a)
                  for a in agents.AGENT_NAMES}
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
        _print_container(name, states[name])


# --------------------------------------------------------------------------
# lxc passthrough
# --------------------------------------------------------------------------

@main.command(
    context_settings=dict(ignore_unknown_options=True,
                          allow_interspersed_args=False),
    add_help_option=False,
)
@click.argument("rest", nargs=-1, type=click.UNPROCESSED)
def lxc(rest):
    """Run lxc against the 'aiab' project (e.g. aiab lxc list)."""
    result = subprocess.run(lxd.lxc_argv(list(rest)))
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
