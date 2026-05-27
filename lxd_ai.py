# lxd_ai.py - shared helpers for lxd-claude, lxd-copilot, etc.
#
# Each tool script calls main() with its own configuration; everything else
# is handled here.

import hashlib
import json
import os
import re
import subprocess
import sys

BASE_CONTAINER = "claude"


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def lxc_exec(container, cmd, **kwargs):
    return run(["lxc", "exec", container, "--"] + cmd, **kwargs)


def container_name_for_dir(path, prefix=BASE_CONTAINER):
    """Return a stable, human-readable LXD container name for a directory."""
    h = hashlib.md5(path.encode()).hexdigest()[:6]
    basename = re.sub(r'[^a-z0-9]+', '-', os.path.basename(path).lower()).strip('-')
    return f"{prefix}-{h}-{basename[:49]}"


def _device_name(path):
    digest = hashlib.md5(path.encode()).hexdigest()[:8]
    return f"dir-{digest}"


def _get_devices(container):
    """Return the devices dict for a container."""
    result = run(
        ["lxc", "query", f"/1.0/instances/{container}"],
        capture_output=True, text=True,
    )
    return json.loads(result.stdout).get("devices", {})


def get_device_paths(container):
    """Return the set of container paths already occupied by disk devices."""
    return {
        dev["path"]
        for dev in _get_devices(container).values()
        if dev.get("type") == "disk" and "path" in dev
    }


def add_device(container, host_path, work_prefix=None):
    name = _device_name(host_path)

    # If the device is already configured for this host path, reuse its
    # existing container path. Computing a fresh path here would see the
    # device's own mount as "occupied" and bump to a non-existent suffixed
    # path (e.g. /work/foo-2), leaving the caller with a bad cwd.
    devices = _get_devices(container)
    if name in devices and devices[name].get("source") == host_path:
        return devices[name]["path"]

    if work_prefix is None:
        container_path = host_path
    else:
        base = os.path.basename(host_path)
        candidate = f"{work_prefix}/{base}"
        occupied = get_device_paths(container)
        if candidate in occupied:
            suffix = 2
            while f"{candidate}-{suffix}" in occupied:
                suffix += 1
            candidate = f"{candidate}-{suffix}"
        container_path = candidate

    # Remove any leftover device from a previous crashed session, then add.
    subprocess.run(
        ["lxc", "config", "device", "remove", container, name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    run(["lxc", "config", "device", "add", container, name,
         "disk", f"source={host_path}", f"path={container_path}"],
        stdout=subprocess.DEVNULL)
    print(f"Mounted {host_path} -> container:{container_path}", file=sys.stderr)
    return container_path


def remove_all_dir_devices(container):
    """Remove all dir-* devices from a container (cleanup on session exit)."""
    result = subprocess.run(
        ["lxc", "config", "device", "show", container],
        capture_output=True, text=True,
    )
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
                    config_device_name, install_cmds, container_user=0):
    """Create and configure a fresh container, then install the tool.

    install_cmds is a list of (description, cmd) pairs where cmd is passed
    to lxc_exec.
    """
    uid = os.getuid()
    gid = os.getgid()

    print(f"Creating container '{container}' from ubuntu:24.04 ...",
          file=sys.stderr)
    run(["lxc", "init", "ubuntu:24.04", container])

    # Map the host user's UID/GID to the container user so that files created
    # inside mounted directories appear owned by the host user.
    run(["lxc", "config", "set", container, "raw.idmap",
         f"uid {uid} {container_user}\ngid {gid} {container_user}"])

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
                         config_device_name, install_cmds, container_user=0,
                         base_container=BASE_CONTAINER):
    """Create a base template container, install tools, then stop it."""
    setup_container(base_container, config_host_dir, config_container_path,
                    config_device_name, install_cmds, container_user)
    print(f"Stopping base container '{base_container}' (template) ...",
          file=sys.stderr)
    run(["lxc", "stop", base_container])


def ensure_session_container(name, base_container=BASE_CONTAINER):
    """Clone from the base container if needed, then ensure it's running."""
    if not container_exists(name):
        print(f"Creating session container '{name}' from base ...",
              file=sys.stderr)
        run(["lxc", "copy", base_container, name])
        run(["lxc", "start", name])
    elif container_status(name) != "RUNNING":
        print(f"Starting container '{name}' ...", file=sys.stderr)
        run(["lxc", "start", name])


def _parse_args(tool_name, config_host_dir, container_label):
    also_dirs = []
    tool_args = []
    shell = False
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
Usage: {prog} [--also DIR]... [--shell] [-- {tool_name.upper()}_ARGS...]

Mounts the current directory into the LXD container {container_label} and runs
{tool_name} in that directory inside the container.

The base container is created automatically on first use (Ubuntu 24.04).
Authenticate inside the container on first run; credentials are stored in
{config_host_dir} and reused in future sessions.

Options:
  --also DIR    Also mount DIR into the container (repeatable)
  --shell       Open an interactive shell in the container instead of running
                {tool_name}
  -h, --help    Show this help

Arguments after -- are passed directly to {tool_name}.""")
            sys.exit(0)
        elif arg == "--also":
            if i + 1 >= len(argv):
                sys.exit("Error: --also requires a directory argument")
            also_dirs.append(os.path.realpath(argv[i + 1]))
            i += 2
            continue
        elif arg == "--shell":
            shell = True
        else:
            tool_args.append(arg)
        i += 1

    return also_dirs, tool_args, shell


def main(config_host_dir, config_container_path,
         config_device_name, command, install_cmds,
         container=None, skip_permissions=False,
         container_user=0, container_home="/root", work_prefix=None,
         base_container=BASE_CONTAINER):
    """Top-level entry point for a tool script.

    Args:
        config_host_dir:       Host path for persistent tool config/auth.
        config_container_path: Where config_host_dir is mounted in container.
        config_device_name:    LXD device name for the config mount.
        command:               Binary to run inside the container.
        install_cmds:          List of (description, cmd) pairs to run during
                               container setup.
        container:             LXD container name. If None, a per-directory
                               container is used (cloned from the base container).
        skip_permissions:      Pass --dangerously-skip-permissions to the tool.
        container_user:        UID to run the command as (default 0 = root).
        container_home:        HOME directory for container_user (default /root).
        work_prefix:           Container directory prefix for mounted paths.
                               If set, each host path is mounted at
                               work_prefix/basename(host_path) (with a numeric
                               suffix to avoid collisions). If None (default),
                               host paths are mirrored at the same absolute path
                               inside the container.
        base_container:        Name of the LXD base/template container to clone
                               session containers from (default: 'claude').
    """
    tool_name = os.path.basename(command)
    cwd = os.getcwd()

    if container is None:
        session_container = container_name_for_dir(cwd, prefix=base_container)
        also_dirs, tool_args, shell = _parse_args(
            tool_name, config_host_dir,
            f"'{session_container}' (derived from current directory)")

        if not container_exists(base_container):
            setup_base_container(config_host_dir, config_container_path,
                                 config_device_name, install_cmds, container_user,
                                 base_container=base_container)
        ensure_session_container(session_container, base_container=base_container)
    else:
        session_container = container
        also_dirs, tool_args, shell = _parse_args(
            tool_name, config_host_dir, f"'{container}'")

        if not container_exists(container):
            setup_container(container, config_host_dir, config_container_path,
                            config_device_name, install_cmds, container_user)
        elif container_status(container) != "RUNNING":
            print(f"Starting container '{container}' ...", file=sys.stderr)
            run(["lxc", "start", container])

    if shell:
        run_cmd = ["bash", "-l"]
    else:
        if skip_permissions:
            tool_args = ["--dangerously-skip-permissions"] + tool_args
        run_cmd = [command] + tool_args

    container_cwd = add_device(session_container, cwd,
                               work_prefix=work_prefix)
    for d in also_dirs:
        add_device(session_container, d, work_prefix=work_prefix)

    exec_cmd = ["lxc", "exec", session_container, f"--cwd={container_cwd}"]
    if container_user != 0:
        exec_cmd += [f"--user={container_user}",
                     f"--env=HOME={container_home}"]
    result = subprocess.run(exec_cmd + ["--"] + run_cmd)
    sys.exit(result.returncode)
