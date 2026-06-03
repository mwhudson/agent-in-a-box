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
# One argparse tree with a subcommand per verb (run, remove, mount, unmount,
# upgrade-templates, list, lxc). The engine lives in aiab.lxd and the per-agent
# data in aiab.agents; this module just parses arguments and orchestrates.

import argparse
import os
import subprocess
import sys

from . import PROJECT, CONTAINER_USER, CONTAINER_HOME, WORK_PREFIX
from . import agents
from . import lxd
from . import state
from .migrate import maybe_migrate

CONFIG_CONTAINER_PATH = CONTAINER_HOME  # agent home dir is mounted here


def _realdir(path):
    return os.path.realpath(path) if path else os.getcwd()


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_run(args, passthrough):
    agent = args.agent
    cfg = agents.get(agent)
    config_host_dir = lxd.agent_home_dir(agent)

    # One-time prepare hook (OpenRouter key prompt, opencode permissive config).
    if cfg.prepare:
        cfg.prepare(config_host_dir)

    for_dir = _realdir(args.for_dir)
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

    if args.shell:
        run_cmd = ["bash", "-l"]
    else:
        agent_args = list(passthrough)
        if cfg.skip_permissions:
            agent_args = ["--dangerously-skip-permissions"] + agent_args
        run_cmd = [cfg.command] + agent_args

    container_cwd = lxd.add_device(session, for_dir, work_prefix=WORK_PREFIX)

    # Record any run-time --also mounts for this directory, then (re)apply
    # every mount recorded for it — so a freshly created or cloned container,
    # and other agents started here later, all pick up the same set.
    for d in args.also:
        state.set_mount(for_dir, d, readonly=True)
    for d in args.also_rw:
        state.set_mount(for_dir, d, readonly=False)
    _apply_recorded_mounts(session, for_dir)

    for host_path, overlay_path in cfg.overlays:
        if os.path.exists(host_path):
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

def cmd_remove(args, passthrough):
    target = _realdir(args.for_dir)
    session = lxd.container_name_for_dir(target, prefix=args.agent)
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
        if not os.path.isdir(m["source"]):
            print(f"Warning: recorded mount {m['source']} not found; skipping",
                  file=sys.stderr)
            continue
        lxd.add_device(container, m["source"], work_prefix=WORK_PREFIX,
                       readonly=m["readonly"])


def cmd_mount(args, passthrough):
    for_dir = _realdir(args.for_dir)
    paths = [os.path.realpath(p) for p in args.dirs]

    # Persist for the directory first, so a later run / another agent (and a
    # recreated container) get the same mounts.
    for path in paths:
        state.set_mount(for_dir, path, readonly=args.readonly)

    found = False
    for agent, container in _agent_containers(for_dir):
        found = True
        print(f"=== {agent} ({container}) ===", file=sys.stderr)
        if lxd.container_status(container) != "RUNNING":
            print("Note: container is not running; mounts apply when it next "
                  "starts.", file=sys.stderr)
        for path in paths:
            lxd.add_device(container, path, work_prefix=WORK_PREFIX,
                           readonly=args.readonly)
    if not found:
        print("No containers exist for this directory yet; the mounts are "
              "recorded and will apply when you next run an agent here.",
              file=sys.stderr)


def cmd_unmount(args, passthrough):
    for_dir = _realdir(args.for_dir)
    paths = [os.path.realpath(p) for p in args.dirs]

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

def cmd_upgrade_templates(args, passthrough):
    unknown = [a for a in args.agents if a not in agents.AGENT_NAMES]
    if unknown:
        sys.exit(f"Error: unknown agent(s): {', '.join(unknown)}. "
                 f"Known: {', '.join(agents.AGENT_NAMES)}")
    targets = args.agents or list(agents.AGENT_NAMES)
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


def cmd_list(args, passthrough):
    states = _container_states()
    if args.for_dir:
        for_dir = _realdir(args.for_dir)
        wanted = {lxd.container_name_for_dir(for_dir, prefix=a)
                  for a in agents.AGENT_NAMES}
        names = [n for n in states if n in wanted]
    else:
        # Skip the bare base/template containers (named exactly after an agent);
        # they hold no project mounts.
        names = [n for n in states if n not in agents.AGENT_NAMES]

    if not names:
        where = f" for {_realdir(args.for_dir)}" if args.for_dir else ""
        print(f"No aiab containers{where}.", file=sys.stderr)
        return

    for name in sorted(names):
        _print_container(name, states[name])


# --------------------------------------------------------------------------
# lxc passthrough
# --------------------------------------------------------------------------

def cmd_lxc(args, passthrough):
    # main() splits off a trailing `-- ...` group before argparse; for lxc that
    # separator is meaningful (e.g. `lxc exec NAME -- cmd`), so put it back.
    lxc_args = list(args.rest)
    if passthrough:
        lxc_args += ["--"] + passthrough
    result = subprocess.run(lxd.lxc_argv(lxc_args))
    sys.exit(result.returncode)


# --------------------------------------------------------------------------
# argument parser
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="aiab",
        description="Run coding agents in disposable per-directory LXD "
                    "containers.")
    sub = parser.add_subparsers(dest="command", required=True)

    agent_names = list(agents.AGENT_NAMES)

    p_run = sub.add_parser(
        "run", help="run an agent in a container for the current directory")
    p_run.add_argument("agent", choices=agent_names, help="which agent to run")
    p_run.add_argument(
        "--for", dest="for_dir", metavar="DIR", default=None,
        help="run the agent for DIR (default: current directory)")
    p_run.add_argument(
        "--also", metavar="DIR", action="append", default=[],
        help="also mount DIR (read-only) into the container (repeatable)")
    p_run.add_argument(
        "--also-rw", metavar="DIR", action="append", default=[],
        help="also mount DIR (read-write) into the container (repeatable)")
    p_run.add_argument(
        "--shell", action="store_true",
        help="open an interactive shell instead of running the agent")
    p_run.set_defaults(func=cmd_run)

    p_remove = sub.add_parser(
        "remove", help="delete the session container for a directory")
    p_remove.add_argument("agent", choices=agent_names)
    p_remove.add_argument(
        "--for", dest="for_dir", metavar="DIR", default=None,
        help="target the container for DIR (default: current directory)")
    p_remove.set_defaults(func=cmd_remove)

    p_mount = sub.add_parser(
        "mount", help="mount extra directories into a directory's containers")
    p_mount.add_argument(
        "--for", dest="for_dir", metavar="DIR", default=None,
        help="target the containers for DIR (default: current directory)")
    mode = p_mount.add_mutually_exclusive_group()
    mode.add_argument("--ro", dest="readonly", action="store_true",
                      help="mount read-only (the default)")
    mode.add_argument("--rw", dest="readonly", action="store_false",
                      help="mount read-write (containers can modify them)")
    p_mount.set_defaults(readonly=True)
    p_mount.add_argument("dirs", nargs="+", metavar="DIR",
                         help="host directories to mount")
    p_mount.set_defaults(func=cmd_mount)

    p_unmount = sub.add_parser(
        "unmount", help="remove extra directory mounts from a directory's "
                        "containers")
    p_unmount.add_argument(
        "--for", dest="for_dir", metavar="DIR", default=None,
        help="target the containers for DIR (default: current directory)")
    p_unmount.add_argument("dirs", nargs="+", metavar="DIR",
                           help="host directories to unmount")
    p_unmount.set_defaults(func=cmd_unmount)

    p_upgrade = sub.add_parser(
        "upgrade-templates",
        help="apt upgrade + reinstall the agent in template containers")
    p_upgrade.add_argument(
        "agents", nargs="*", metavar="AGENT", default=[],
        help=f"template(s) to upgrade: {', '.join(agent_names)} "
             "(default: all that exist)")
    p_upgrade.set_defaults(func=cmd_upgrade_templates)

    p_list = sub.add_parser(
        "list", help="list aiab containers with their source dir and mounts")
    p_list.add_argument(
        "--for", dest="for_dir", metavar="DIR", default=None,
        help="show only the containers for DIR")
    p_list.set_defaults(func=cmd_list)

    p_lxc = sub.add_parser(
        "lxc", help=f"run lxc against the '{PROJECT}' project "
                    "(e.g. aiab lxc list)")
    p_lxc.add_argument("rest", nargs=argparse.REMAINDER,
                       help="arguments passed to lxc --project " + PROJECT)
    p_lxc.set_defaults(func=cmd_lxc)

    return parser


def main():
    # Split off a trailing `-- ...` group up front so subcommand options never
    # collide with arguments meant for the agent (run) or lxc (lxc).
    argv = sys.argv[1:]
    if "--" in argv:
        i = argv.index("--")
        head, passthrough = argv[:i], argv[i + 1:]
    else:
        head, passthrough = argv, []

    parser = build_parser()
    args = parser.parse_args(head)

    # Migrate from the old lxd-* layout first, if present: maybe_migrate()
    # keys off whether the 'aiab' project exists yet, so it must run before
    # use_project() (which would create that project and defeat the trigger).
    maybe_migrate()
    lxd.use_project(PROJECT)

    args.func(args, passthrough)
