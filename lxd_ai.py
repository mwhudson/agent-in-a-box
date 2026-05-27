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
# lxd_ai.py - shared helpers for lxd-claude, lxd-copilot, etc.
#
# Each tool script calls main() with its own configuration; everything else
# is handled here.

import hashlib
import os
import re
import subprocess
import sys

import yaml

BASE_CONTAINER = "claude"

# LXD project that all tool containers live in. Set via use_project(); while
# None, lxc commands run against the client's active/default project.
_PROJECT = None


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def _lxc(args):
    """Build an lxc command list targeting the configured project (if any).

    Used for everything that operates on instances/devices in the project.
    Project-management commands (lxc project ...) deliberately bypass this.
    """
    cmd = ["lxc"]
    if _PROJECT is not None:
        cmd += ["--project", _PROJECT]
    return cmd + args


def project_exists(name):
    r = subprocess.run(
        ["lxc", "project", "show", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def use_project(name):
    """Direct all subsequent instance commands at the given LXD project.

    The project is created if missing, configured to share the default
    project's profiles and images so containers get networking/storage from
    the existing 'default' profile and don't re-download base images. Passing
    None leaves commands targeting the client's active project.
    """
    global _PROJECT
    _PROJECT = name
    if name is None or project_exists(name):
        return
    print(f"Creating LXD project '{name}' "
          "(sharing default profiles and images) ...", file=sys.stderr)
    run(["lxc", "project", "create", name,
         "-c", "features.images=false",
         "-c", "features.profiles=false"])


def lxc_exec(container, cmd, **kwargs):
    return run(_lxc(["exec", container, "--"] + cmd), **kwargs)


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
        _lxc(["config", "show", container]),
        capture_output=True, text=True,
    )
    return yaml.safe_load(result.stdout).get("devices", {})


def get_device_paths(container):
    """Return the set of container paths already occupied by disk devices."""
    return {
        dev["path"]
        for dev in _get_devices(container).values()
        if dev.get("type") == "disk" and "path" in dev
    }


def add_device(container, host_path, work_prefix=None, readonly=False):
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
        _lxc(["config", "device", "remove", container, name]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    add_cmd = ["config", "device", "add", container, name,
               "disk", f"source={host_path}", f"path={container_path}"]
    if readonly:
        add_cmd.append("readonly=true")
    run(_lxc(add_cmd), stdout=subprocess.DEVNULL)
    mode = " (read-only)" if readonly else ""
    print(f"Mounted {host_path} -> container:{container_path}{mode}", file=sys.stderr)
    return container_path


def add_config_overlay(container, host_path, container_path):
    """Bind-mount a host file or directory at an explicit container path.

    Used to overlay versioned config (e.g. CLAUDE.md, commands/) onto the
    container's home, independent of the working-directory mounts. The device
    name is derived from container_path so the mount is idempotent across
    sessions, and uses a 'cfg-' prefix so remove_all_dir_devices leaves it
    alone.
    """
    name = "cfg-" + hashlib.md5(container_path.encode()).hexdigest()[:8]
    devices = _get_devices(container)
    existing = devices.get(name)
    if (existing and existing.get("source") == host_path
            and existing.get("path") == container_path):
        return
    subprocess.run(
        _lxc(["config", "device", "remove", container, name]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    run(_lxc(["config", "device", "add", container, name,
              "disk", f"source={host_path}", f"path={container_path}"]),
        stdout=subprocess.DEVNULL)
    print(f"Overlaid {host_path} -> container:{container_path}", file=sys.stderr)


def remove_all_dir_devices(container):
    """Remove all dir-* devices from a container (cleanup on session exit)."""
    result = subprocess.run(
        _lxc(["config", "device", "show", container]),
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and line.endswith(':'):
            name = line[:-1]
            if name.startswith('dir-'):
                subprocess.run(
                    _lxc(["config", "device", "remove", container, name]),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )


def container_exists(container):
    r = subprocess.run(
        _lxc(["info", container]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def container_status(container):
    return run(
        _lxc(["list", container, "--format=csv", "--columns=s"]),
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
    run(_lxc(["init", "ubuntu:24.04", container]))

    # Map the host user's UID/GID to the container user so that files created
    # inside mounted directories appear owned by the host user.
    run(_lxc(["config", "set", container, "raw.idmap",
              f"uid {uid} {container_user}\ngid {gid} {container_user}"]))

    # Mount a dedicated config directory for persistent authentication.
    # On first use the tool will prompt for credentials inside the container.
    os.makedirs(config_host_dir, exist_ok=True)
    run(_lxc(["config", "device", "add", container, config_device_name,
              "disk", f"source={config_host_dir}",
              f"path={config_container_path}"]),
        stdout=subprocess.DEVNULL)
    print(f"Container config: {config_host_dir}", file=sys.stderr)

    print(f"Starting container '{container}' ...", file=sys.stderr)
    run(_lxc(["start", container]))

    print("Waiting for cloud-init ...", file=sys.stderr)
    lxc_exec(container, ["cloud-init", "status", "--wait"],
             stdout=subprocess.DEVNULL)

    print("Updating packages ...", file=sys.stderr)
    lxc_exec(container, ["apt-get", "update", "-q"], stdout=subprocess.DEVNULL)
    lxc_exec(container, ["apt-get", "dist-upgrade", "-y", "-q"],
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
    run(_lxc(["stop", base_container]))


def update_base_container(base_container, update_cmds, container_user=0):
    """Update an existing base template container and stop it again.

    Starts the container, runs apt-get update/dist-upgrade, then runs
    update_cmds (same format as install_cmds), then stops it. If the
    container is not found, exits with an error.
    """
    if not container_exists(base_container):
        print(f"Skipping '{base_container}': container does not exist.",
              file=sys.stderr)
        return False

    if container_status(base_container) != "RUNNING":
        print(f"Starting '{base_container}' ...", file=sys.stderr)
        run(_lxc(["start", base_container]))

    print("Updating packages ...", file=sys.stderr)
    lxc_exec(base_container, ["apt-get", "update", "-q"],
             stdout=subprocess.DEVNULL)
    lxc_exec(base_container, ["apt-get", "dist-upgrade", "-y", "-q"],
             stdout=subprocess.DEVNULL)

    for description, cmd in update_cmds:
        print(description, file=sys.stderr)
        lxc_exec(base_container, cmd, stdout=subprocess.DEVNULL)

    print(f"Stopping '{base_container}' (template) ...", file=sys.stderr)
    run(_lxc(["stop", base_container]))
    print(f"'{base_container}' updated.\n", file=sys.stderr)
    return True


def ensure_session_container(name, base_container=BASE_CONTAINER):
    """Clone from the base container if needed, then ensure it's running."""
    if not container_exists(name):
        print(f"Creating session container '{name}' from base ...",
              file=sys.stderr)
        run(_lxc(["copy", base_container, name]))
        run(_lxc(["start", name]))
    elif container_status(name) != "RUNNING":
        print(f"Starting container '{name}' ...", file=sys.stderr)
        run(_lxc(["start", name]))


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


def _add_wayland_socket(container, container_user):
    """Bind-mount the host Wayland socket into the container.

    Reads WAYLAND_DISPLAY and XDG_RUNTIME_DIR from the host environment,
    mounts the socket at the same path inside the container, and returns a
    list of --env flags to pass to lxc exec. Returns an empty list if the
    host environment is not set up for Wayland.
    """
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
    if not xdg_runtime_dir:
        print("Warning: XDG_RUNTIME_DIR not set; skipping Wayland passthrough",
              file=sys.stderr)
        return []

    socket_host = os.path.join(xdg_runtime_dir, wayland_display)
    if not os.path.exists(socket_host):
        print(f"Warning: Wayland socket {socket_host} not found; "
              "skipping Wayland passthrough", file=sys.stderr)
        return []

    # Mirror the socket at the same path inside the container so that
    # XDG_RUNTIME_DIR and WAYLAND_DISPLAY need no adjustment.
    socket_container = socket_host
    name = "wayland-" + hashlib.md5(socket_host.encode()).hexdigest()[:8]
    devices = _get_devices(container)
    existing = devices.get(name)
    if not (existing and existing.get("source") == socket_host
            and existing.get("path") == socket_container):
        # Ensure the parent directory exists inside the container, owned by
        # container_user. Run mkdir as root so it can create /run/user/NNN,
        # then chown to the target user.
        subprocess.run(
            _lxc(["exec", container, "--",
                  "bash", "-c",
                  f"mkdir -p {xdg_runtime_dir} && "
                  f"chown {container_user}:{container_user} {xdg_runtime_dir} && "
                  f"chmod 700 {xdg_runtime_dir}"]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            _lxc(["config", "device", "remove", container, name]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        run(_lxc(["config", "device", "add", container, name,
                  "disk", f"source={socket_host}", f"path={socket_container}"]),
            stdout=subprocess.DEVNULL)
        print(f"Mounted Wayland socket {socket_host} -> container:{socket_container}",
              file=sys.stderr)

    return [
        f"--env=WAYLAND_DISPLAY={wayland_display}",
        f"--env=XDG_RUNTIME_DIR={xdg_runtime_dir}",
    ]


def main(config_host_dir, config_container_path,
         config_device_name, command, install_cmds,
         container=None, skip_permissions=False,
         container_user=0, container_home="/root", work_prefix=None,
         base_container=BASE_CONTAINER, config_overlays=None, project=None,
         wayland_passthrough=False):
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
        config_overlays:       Optional list of (host_path, container_path)
                               pairs to bind-mount into the session container
                               before running the tool (e.g. versioned
                               CLAUDE.md / commands/). Entries whose host_path
                               does not exist are skipped.
        project:               LXD project to place containers in. Created
                               (sharing the default project's profiles and
                               images) if missing. None uses the active project.
        wayland_passthrough:   If True, bind-mount the host Wayland socket into
                               the container and set WAYLAND_DISPLAY and
                               XDG_RUNTIME_DIR in the exec environment, enabling
                               clipboard integration via wl-clipboard.
    """
    tool_name = os.path.basename(command)
    cwd = os.getcwd()

    use_project(project)

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
            run(_lxc(["start", container]))

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

    for host_path, overlay_path in (config_overlays or []):
        if os.path.exists(host_path):
            add_config_overlay(session_container, host_path, overlay_path)

    exec_cmd = _lxc(["exec", session_container, f"--cwd={container_cwd}"])
    if container_user != 0:
        exec_cmd += [f"--user={container_user}",
                     f"--env=HOME={container_home}"]
    if wayland_passthrough:
        exec_cmd += _add_wayland_socket(session_container, container_user)
    result = subprocess.run(exec_cmd + ["--"] + run_cmd)
    sys.exit(result.returncode)
