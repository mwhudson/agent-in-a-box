# lxd_ai.py - shared helpers for lxd-claude, lxd-copilot, etc.
#
# Each tool script calls main() with its own configuration; everything else
# is handled here.

import hashlib
import json
import os
import subprocess
import sys


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def lxc_exec(container, cmd, **kwargs):
    return run(["lxc", "exec", container, "--"] + cmd, **kwargs)


def _device_name(path):
    digest = hashlib.md5(path.encode()).hexdigest()[:8]
    return f"dir-{digest}"


def get_device_paths(container):
    """Return the set of container paths already occupied by disk devices."""
    result = run(
        ["lxc", "query", f"/1.0/instances/{container}"],
        capture_output=True, text=True,
    )
    data = json.loads(result.stdout)
    return {
        dev["path"]
        for dev in data.get("devices", {}).values()
        if dev.get("type") == "disk" and "path" in dev
    }


def add_device(container, host_path, devices, work_prefix=None):
    name = _device_name(host_path)
    # Remove any leftover device from a previous crashed session
    subprocess.run(
        ["lxc", "config", "device", "remove", container, name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
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
    run(["lxc", "config", "device", "add", container, name,
         "disk", f"source={host_path}", f"path={container_path}"],
        stdout=subprocess.DEVNULL)
    devices.append(name)
    print(f"Mounted {host_path} -> container:{container_path}", file=sys.stderr)
    return container_path


def remove_devices(container, devices):
    for name in devices:
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

    # All configuration is done before first boot; start the container once.
    print(f"Starting container '{container}' ...", file=sys.stderr)
    run(["lxc", "start", container])

    print("Waiting for cloud-init ...", file=sys.stderr)
    lxc_exec(container, ["cloud-init", "status", "--wait"],
             stdout=subprocess.DEVNULL)

    for description, cmd in install_cmds:
        print(description, file=sys.stderr)
        lxc_exec(container, cmd, stdout=subprocess.DEVNULL)

    print(f"Container '{container}' is ready.\n", file=sys.stderr)


def _parse_args(tool_name, container, config_host_dir):
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

Mounts the current directory into the LXD container '{container}' and runs
{tool_name} in that directory inside the container.

The container is created automatically on first use (Ubuntu 24.04).
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


def main(container, config_host_dir, config_container_path,
         config_device_name, command, install_cmds,
         container_user=0, container_home="/root", work_prefix=None):
    """Top-level entry point for a tool script.

    Args:
        container:             LXD container name.
        config_host_dir:       Host path for persistent tool config/auth.
        config_container_path: Where config_host_dir is mounted in container.
        config_device_name:    LXD device name for the config mount.
        command:               Binary to run inside the container.
        install_cmds:          List of (description, cmd) pairs to run during
                               container setup.
        container_user:        UID to run the command as (default 0 = root).
        container_home:        HOME directory for container_user (default /root).
        work_prefix:           Container directory prefix for mounted paths.
                               If set, each host path is mounted at
                               work_prefix/basename(host_path) (with a numeric
                               suffix to avoid collisions). If None (default),
                               host paths are mirrored at the same absolute path
                               inside the container.
    """
    also_dirs, tool_args = _parse_args(command, container, config_host_dir)
    cwd = os.getcwd()
    devices = []

    if not container_exists(container):
        setup_container(container, config_host_dir, config_container_path,
                        config_device_name, install_cmds, container_user)
    elif container_status(container) != "RUNNING":
        print(f"Starting container '{container}' ...", file=sys.stderr)
        run(["lxc", "start", container])

    try:
        container_cwd = add_device(container, cwd, devices,
                                   work_prefix=work_prefix)
        for d in also_dirs:
            add_device(container, d, devices, work_prefix=work_prefix)

        exec_cmd = ["lxc", "exec", container, f"--cwd={container_cwd}"]
        if container_user != 0:
            exec_cmd += [f"--user={container_user}",
                         f"--env=HOME={container_home}"]
        result = subprocess.run(exec_cmd + ["--", command] + tool_args)
        sys.exit(result.returncode)
    finally:
        remove_devices(container, devices)
