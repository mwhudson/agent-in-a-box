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
# aiab.lxd - the LXD plumbing shared by every aiab subcommand.
#
# Everything that talks to `lxc` lives here: project setup, container
# naming/lifecycle, device (mount) management, config overlays, and Wayland
# passthrough. The cli module orchestrates these; the agents module supplies
# the per-agent data.

import hashlib
import os
import re
import subprocess
import sys

import yaml

# LXD project that all instance/device commands are routed at. Set via
# use_project(); while None, lxc commands run against the client's active
# project (used by project-management commands, which deliberately bypass it).
_PROJECT = None


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def lxc_argv(args):
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
    return run(lxc_argv(["exec", container, "--"] + cmd), **kwargs)


def agent_home_dir(agent):
    """Host dir bind-mounted as the agent container's home (/home/ubuntu).

    Holds the agent's persistent config/auth between sessions. Callers that
    need to pre-seed config (e.g. a default settings file) write into here
    before launching the agent.
    """
    return os.path.expanduser(f"~/.local/share/aiab/{agent}/home")


def container_name_for_dir(path, prefix):
    """Return a stable, human-readable LXD container name for a directory.

    Shaped <prefix>-<basename>-<hash>: the basename comes first so tab
    completion keys off the memorable part, with a short path hash appended to
    disambiguate same-named directories in different locations.
    """
    h = hashlib.md5(path.encode()).hexdigest()[:6]
    basename = re.sub(r'[^a-z0-9]+', '-', os.path.basename(path).lower()).strip('-')
    return f"{prefix}-{basename[:49]}-{h}"


def _device_name(path):
    digest = hashlib.md5(path.encode()).hexdigest()[:8]
    return f"dir-{digest}"


def _get_devices(container):
    """Return the devices dict for a container."""
    result = run(
        lxc_argv(["config", "show", container]),
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
    # path (e.g. /work/foo-2), leaving the caller with a bad cwd. Reconcile
    # the readonly flag in place if it changed (e.g. re-mounting --ro/--rw).
    devices = _get_devices(container)
    existing = devices.get(name)
    if existing and existing.get("source") == host_path:
        container_path = existing["path"]
        current_ro = str(existing.get("readonly", "false")).lower() == "true"
        if current_ro != readonly:
            run(lxc_argv(["config", "device", "set", container, name,
                          "readonly", "true" if readonly else "false"]),
                stdout=subprocess.DEVNULL)
            mode = "read-only" if readonly else "read-write"
            print(f"Set {host_path} -> container:{container_path} to {mode}",
                  file=sys.stderr)
        return container_path

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
        lxc_argv(["config", "device", "remove", container, name]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    add_cmd = ["config", "device", "add", container, name,
               "disk", f"source={host_path}", f"path={container_path}"]
    if readonly:
        add_cmd.append("readonly=true")
    run(lxc_argv(add_cmd), stdout=subprocess.DEVNULL)
    mode = " (read-only)" if readonly else ""
    print(f"Mounted {host_path} -> container:{container_path}{mode}", file=sys.stderr)
    return container_path


def remove_dir_device(container, host_path):
    """Remove the dir-* mount for host_path from a container, if present.

    Returns True if a device was removed, False if there was nothing mounted
    for that path. Used by `aiab unmount`.
    """
    name = _device_name(host_path)
    if name not in _get_devices(container):
        return False
    run(lxc_argv(["config", "device", "remove", container, name]),
        stdout=subprocess.DEVNULL)
    return True


def add_config_overlay(container, host_path, container_path, container_user=0):
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
    # Create the mountpoint's parent dirs as container_user first. Otherwise
    # LXD creates any missing parents as container root, which falls outside
    # the idmap and shows up unreadable on the host.
    parent = os.path.dirname(container_path)
    if parent and parent != "/" and container_user != 0:
        subprocess.run(
            lxc_argv(["exec", container,
                      f"--user={container_user}", f"--group={container_user}",
                      "--", "mkdir", "-p", parent]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    subprocess.run(
        lxc_argv(["config", "device", "remove", container, name]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    run(lxc_argv(["config", "device", "add", container, name,
                  "disk", f"source={host_path}", f"path={container_path}"]),
        stdout=subprocess.DEVNULL)
    print(f"Overlaid {host_path} -> container:{container_path}", file=sys.stderr)


def remove_all_dir_devices(container):
    """Remove all dir-* devices from a container (cleanup on session exit)."""
    for name in _get_devices(container):
        if name.startswith('dir-'):
            subprocess.run(
                lxc_argv(["config", "device", "remove", container, name]),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


def remove_session_container(container):
    """Delete a container (and its mounts), as requested by `aiab remove`.

    Targets only the named session container, leaving the base/template
    container intact so a fresh one can be cloned quickly next time.
    """
    if not container_exists(container):
        print(f"No container '{container}' to remove.", file=sys.stderr)
        return
    print(f"Removing container '{container}' ...", file=sys.stderr)
    run(lxc_argv(["delete", "--force", container]))
    print(f"Removed container '{container}'.", file=sys.stderr)


def container_exists(container):
    r = subprocess.run(
        lxc_argv(["info", container]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def container_status(container):
    return run(
        lxc_argv(["list", container, "--format=csv", "--columns=s"]),
        capture_output=True, text=True,
    ).stdout.strip()


def setup_container(container, config_host_dir, config_container_path,
                    config_device_name, install_cmds, container_user=0):
    """Create and configure a fresh container, then install the agent.

    install_cmds is a list of (description, cmd) pairs where cmd is passed
    to lxc_exec.
    """
    uid = os.getuid()
    gid = os.getgid()

    print(f"Creating container '{container}' from ubuntu:24.04 ...",
          file=sys.stderr)
    run(lxc_argv(["init", "ubuntu:24.04", container]))

    # Map the host user's UID/GID to the container user so that files created
    # inside mounted directories appear owned by the host user.
    run(lxc_argv(["config", "set", container, "raw.idmap",
                  f"uid {uid} {container_user}\ngid {gid} {container_user}"]))

    # Mount a dedicated config directory for persistent authentication.
    # On first use the agent will prompt for credentials inside the container.
    os.makedirs(config_host_dir, exist_ok=True)
    run(lxc_argv(["config", "device", "add", container, config_device_name,
                  "disk", f"source={config_host_dir}",
                  f"path={config_container_path}"]),
        stdout=subprocess.DEVNULL)
    print(f"Container config: {config_host_dir}", file=sys.stderr)

    print(f"Starting container '{container}' ...", file=sys.stderr)
    run(lxc_argv(["start", container]))

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


def setup_base_container(base_container, config_host_dir, config_container_path,
                         config_device_name, install_cmds, container_user=0):
    """Create a base template container, install the agent, then stop it."""
    setup_container(base_container, config_host_dir, config_container_path,
                    config_device_name, install_cmds, container_user)
    print(f"Stopping base container '{base_container}' (template) ...",
          file=sys.stderr)
    run(lxc_argv(["stop", base_container]))


def update_base_container(base_container, update_cmds, container_user=0):
    """Update an existing base template container and stop it again.

    Starts the container, runs apt-get update/dist-upgrade, then runs
    update_cmds (same format as install_cmds), then stops it. Returns False
    (and prints a note) if the container does not exist.
    """
    if not container_exists(base_container):
        print(f"Skipping '{base_container}': container does not exist.",
              file=sys.stderr)
        return False

    if container_status(base_container) != "RUNNING":
        print(f"Starting '{base_container}' ...", file=sys.stderr)
        run(lxc_argv(["start", base_container]))

    print("Updating packages ...", file=sys.stderr)
    lxc_exec(base_container, ["apt-get", "update", "-q"],
             stdout=subprocess.DEVNULL)
    lxc_exec(base_container, ["apt-get", "dist-upgrade", "-y", "-q"],
             stdout=subprocess.DEVNULL)

    for description, cmd in update_cmds:
        print(description, file=sys.stderr)
        lxc_exec(base_container, cmd, stdout=subprocess.DEVNULL)

    print(f"Stopping '{base_container}' (template) ...", file=sys.stderr)
    run(lxc_argv(["stop", base_container]))
    print(f"'{base_container}' updated.\n", file=sys.stderr)
    return True


def ensure_session_container(name, base_container):
    """Clone from the base container if needed, then ensure it's running."""
    if not container_exists(name):
        print(f"Creating session container '{name}' from base ...",
              file=sys.stderr)
        run(lxc_argv(["copy", base_container, name]))
        run(lxc_argv(["start", name]))
    elif container_status(name) != "RUNNING":
        print(f"Starting container '{name}' ...", file=sys.stderr)
        run(lxc_argv(["start", name]))


def add_wayland_socket(container, container_user):
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
            lxc_argv(["exec", container, "--",
                      "bash", "-c",
                      f"mkdir -p {xdg_runtime_dir} && "
                      f"chown {container_user}:{container_user} {xdg_runtime_dir} && "
                      f"chmod 700 {xdg_runtime_dir}"]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            lxc_argv(["config", "device", "remove", container, name]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        run(lxc_argv(["config", "device", "add", container, name,
                      "disk", f"source={socket_host}", f"path={socket_container}"]),
            stdout=subprocess.DEVNULL)
        print(f"Mounted Wayland socket {socket_host} -> container:{socket_container}",
              file=sys.stderr)

    return [
        f"--env=WAYLAND_DISPLAY={wayland_display}",
        f"--env=XDG_RUNTIME_DIR={xdg_runtime_dir}",
    ]
