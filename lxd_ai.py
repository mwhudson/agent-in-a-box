# lxd_ai.py - shared helpers for lxd-claude, lxd-copilot, etc.
#
# Each tool script calls main() with its own configuration; everything else
# is handled here.

import hashlib
import os
import re
import subprocess
import sys

BASE_CONTAINER = "claude"


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def lxc_exec(container, cmd, **kwargs):
    return run(["lxc", "exec", container, "--"] + cmd, **kwargs)


def container_name_for_dir(path):
    """Return a stable, human-readable LXD container name for a directory."""
    h = hashlib.md5(path.encode()).hexdigest()[:6]
    basename = re.sub(r'[^a-z0-9]+', '-', os.path.basename(path).lower()).strip('-')
    return f"claude-{h}-{basename[:49]}"


def _device_name(path):
    digest = hashlib.md5(path.encode()).hexdigest()[:8]
    return f"dir-{digest}"


def add_device(container, path, devices=None):
    name = _device_name(path)
    # Remove any leftover device from a previous crashed session
    subprocess.run(
        ["lxc", "config", "device", "remove", container, name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    run(["lxc", "config", "device", "add", container, name,
         "disk", f"source={path}", f"path={path}"],
        stdout=subprocess.DEVNULL)
    if devices is not None:
        devices.append(name)
    print(f"Mounted {path} -> container:{path}", file=sys.stderr)


def remove_all_dir_devices(container):
    """Remove all dir-* devices from a container (cleanup on session exit)."""
    result = subprocess.run(
        ["lxc", "config", "device", "show", container],
        capture_output=True, text=True,
    )
    # Parse top-level YAML keys — device names appear as "name:" at column 0
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and line.endswith(':'):
            name = line[:-1]
            if name.startswith('dir-'):
                subprocess.run(
                    ["lxc", "config", "device", "remove", container, name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )


def container_exists(container):
    r = subprocess.run(
        ["lxc", "info", container],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def container_status(container):
    return run(
        ["lxc", "list", container, "--format=csv", "--columns=s"],
        capture_output=True, text=True,
    ).stdout.strip()


def setup_container(container, config_host_dir, config_container_path,
                    config_device_name, install_cmds):
    """Create and configure a fresh container, then install the tool."""
    uid = os.getuid()
    gid = os.getgid()

    print(f"Creating container '{container}' from ubuntu:24.04 ...",
          file=sys.stderr)
    run(["lxc", "init", "ubuntu:24.04", container])

    # Map the host user's UID/GID to container root so that files created
    # inside mounted directories appear owned by the host user.
    run(["lxc", "config", "set", container, "raw.idmap",
         f"uid {uid} 0\ngid {gid} 0"])

    # Mount a dedicated config directory for persistent authentication.
    # On first use the tool will prompt for credentials inside the container.
    os.makedirs(config_host_dir, exist_ok=True)
    run(["lxc", "config", "device", "add", container, config_device_name,
         "disk", f"source={config_host_dir}", f"path={config_container_path}"],
        stdout=subprocess.DEVNULL)
    print(f"Container config: {config_host_dir}", file=sys.stderr)

    print(f"Starting container '{container}' ...", file=sys.stderr)
    run(["lxc", "start", container])

    print("Waiting for cloud-init ...", file=sys.stderr)
    lxc_exec(container, ["cloud-init", "status", "--wait"],
             stdout=subprocess.DEVNULL)

    for description, cmd in install_cmds:
        print(description, file=sys.stderr)
        lxc_exec(container, cmd, stdout=subprocess.DEVNULL)

    print(f"Container '{container}' is ready.\n", file=sys.stderr)


def setup_base_container(config_host_dir, config_container_path,
                         config_device_name, install_cmds):
    """Create the base 'claude' template container, install tools, then stop it."""
    setup_container(BASE_CONTAINER, config_host_dir, config_container_path,
                    config_device_name, install_cmds)
    print(f"Stopping base container '{BASE_CONTAINER}' (template) ...",
          file=sys.stderr)
    run(["lxc", "stop", BASE_CONTAINER])


def ensure_session_container(name):
    """Clone from the base container if needed, then ensure it's running."""
    if not container_exists(name):
        print(f"Creating session container '{name}' from base ...",
              file=sys.stderr)
        run(["lxc", "copy", BASE_CONTAINER, name])
        run(["lxc", "start", name])
    elif container_status(name) != "RUNNING":
        print(f"Starting container '{name}' ...", file=sys.stderr)
        run(["lxc", "start", name])


def _parse_args(tool_name, config_host_dir, container_label):
    also_dirs = []
    tool_args = []
    argv = sys.argv[1:]

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            tool_args = argv[i + 1:]
            break
        elif arg in ("-h", "--help"):
            prog = os.path.basename(sys.argv[0])
            print(f"""\
Usage: {prog} [--also DIR]... [-- {tool_name.upper()}_ARGS...]

Mounts the current directory into the LXD container {container_label} and runs
{tool_name} in that directory inside the container.

The base container is created automatically on first use (Ubuntu 24.04).
Authenticate inside the container on first run; credentials are stored in
{config_host_dir} and reused in future sessions.

Options:
  --also DIR    Also mount DIR into the container (repeatable)
  -h, --help    Show this help

Arguments after -- are passed directly to {tool_name}.""")
            sys.exit(0)
        elif arg == "--also":
            if i + 1 >= len(argv):
                sys.exit("Error: --also requires a directory argument")
            also_dirs.append(os.path.realpath(argv[i + 1]))
            i += 2
            continue
        else:
            tool_args.append(arg)
        i += 1

    return also_dirs, tool_args


def main(config_host_dir, config_container_path,
         config_device_name, command, install_cmds,
         container=None, skip_permissions=False):
    """Top-level entry point for a tool script.

    Args:
        config_host_dir:       Host path for persistent tool config/auth.
        config_container_path: Where config_host_dir is mounted in container.
        config_device_name:    LXD device name for the config mount.
        command:               Binary to run inside the container.
        install_cmds:          List of (description, cmd) pairs to run during
                               container setup.
        container:             LXD container name. If None, a per-directory
                               container is used (cloned from the base 'claude').
        skip_permissions:      Pass --dangerously-skip-permissions to the tool.
    """
    tool_name = os.path.basename(command)
    cwd = os.getcwd()

    if container is None:
        session_container = container_name_for_dir(cwd)
        also_dirs, tool_args = _parse_args(
            tool_name, config_host_dir,
            f"'{session_container}' (derived from current directory)")

        if not container_exists(BASE_CONTAINER):
            setup_base_container(config_host_dir, config_container_path,
                                 config_device_name, install_cmds)
        ensure_session_container(session_container)
    else:
        session_container = container
        also_dirs, tool_args = _parse_args(
            tool_name, config_host_dir, f"'{container}'")

        if not container_exists(container):
            setup_container(container, config_host_dir, config_container_path,
                            config_device_name, install_cmds)
        elif container_status(container) != "RUNNING":
            print(f"Starting container '{container}' ...", file=sys.stderr)
            run(["lxc", "start", container])

    if skip_permissions:
        tool_args = ["--dangerously-skip-permissions"] + tool_args

    try:
        add_device(session_container, cwd)
        for d in also_dirs:
            add_device(session_container, d)

        result = subprocess.run(
            ["lxc", "exec", session_container, f"--cwd={cwd}", "--", command]
            + tool_args,
        )
        sys.exit(result.returncode)
    finally:
        remove_all_dir_devices(session_container)
