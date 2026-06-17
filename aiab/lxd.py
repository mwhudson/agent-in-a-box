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
# Two objects wrap the `lxc` CLI. Lxd is a connection scoped to one project: it
# builds project-targeted argv, manages the project, and hands out Container
# handles. Container wraps a single instance — lifecycle, exec, and device
# (mount) management. Agent provisioning (installing the agents, apt upgrades)
# lives in aiab.provision, which drives these, so this module stays a generic
# LXD wrapper that knows nothing about agents. The cli module orchestrates it
# all; the agents module supplies the per-agent data.

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from . import StrPath


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kwargs)


def agent_home_dir(agent: str) -> Path:
    """Host dir bind-mounted as the agent container's home (/home/ubuntu).

    Holds the agent's persistent config/auth between sessions. Callers that
    need to pre-seed config (e.g. a default settings file) write into here
    before launching the agent.
    """
    return Path.home() / ".local" / "share" / "aiab" / agent / "home"


def dir_slug(path: StrPath) -> str:
    """Return a stable, human-readable identifier for a directory.

    Shaped <basename>-<hash>: the basename comes first so the memorable part
    leads (and tab completion keys off it), with a short path hash appended to
    disambiguate same-named directories in different locations. Used both for
    container names (see container_name_for_dir) and for the per-directory
    state dirs in aiab.state, so the two are easy to correlate.
    """
    path = Path(path)
    h = hashlib.md5(str(path).encode()).hexdigest()[:6]
    basename = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")
    return f"{basename[:49]}-{h}"


def container_name_for_dir(path: StrPath, prefix: str) -> str:
    """Return a stable LXD container name for a directory: <prefix>-<slug>."""
    return f"{prefix}-{dir_slug(path)}"


def _device_name(path: StrPath) -> str:
    digest = hashlib.md5(os.fspath(path).encode()).hexdigest()[:8]
    return f"dir-{digest}"


def is_source_device(device_name: str, container_name: str) -> bool:
    """Return True if device_name is the source-directory device for container_name.

    Container names embed a 6-char path hash as their last component
    (see container_name_for_dir); source-directory device names are
    'dir-{md5[:8]}' of the same path (see _device_name).  The device
    hash is longer, so we do a prefix match rather than equality — both
    share the same leading 6 hex chars.
    """
    path_hash = container_name.rsplit("-", 1)[-1]
    return device_name.startswith(f"dir-{path_hash}")


class Lxd:
    """A connection to LXD scoped to a single project.

    Builds project-targeted `lxc` argv and manages the project itself; use
    container()/container_for_dir() to get Container handles. Holding the
    project on the instance (rather than in a module global) keeps it explicit
    which project a command targets — and lets the migration drive the old and
    new projects side by side.
    """

    def __init__(self, project: str) -> None:
        self.project = project

    def argv(self, args: list[str]) -> list[str]:
        """Build an `lxc --project <project> ...` argv."""
        return ["lxc", "--project", self.project, *args]

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Run an `lxc` command in this project (checked)."""
        return run(self.argv(args), **kwargs)

    def project_exists(self) -> bool:
        r = subprocess.run(
            ["lxc", "project", "show", self.project],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0

    def ensure_project(self) -> None:
        """Create the project if missing.

        Configured to share the default project's profiles and images so
        containers get networking/storage from the existing 'default' profile
        and don't re-download base images.
        """
        if self.project_exists():
            return
        print(
            f"Creating LXD project '{self.project}' "
            "(sharing default profiles and images) ...",
            file=sys.stderr,
        )
        run(
            [
                "lxc",
                "project",
                "create",
                self.project,
                "-c",
                "features.images=false",
                "-c",
                "features.profiles=false",
            ]
        )

    def delete_project(self) -> None:
        run(["lxc", "project", "delete", self.project])

    def container(self, name: str) -> Container:
        return Container(self, name)

    def container_for_dir(self, path: StrPath, prefix: str) -> Container:
        return Container(self, container_name_for_dir(path, prefix))

    def profile_nic_names(self, profile: str = "default") -> list[str]:
        """Return the names of the NIC devices a profile provides.

        Containers in the aiab project inherit their network from the shared
        'default' profile (the project is created with features.profiles=false);
        these are the device names to mask when cutting off direct egress.
        """
        result = self.run(["profile", "show", profile], capture_output=True, text=True)
        devices = yaml.safe_load(result.stdout).get("devices", {})
        return [n for n, d in devices.items() if d.get("type") == "nic"]

    def instances(self) -> dict[str, str]:
        """Return a {name: status} map for every instance in the project."""
        result = run(
            self.argv(["list", "--format=csv", "--columns=n,s"]),
            capture_output=True,
            text=True,
        )
        states = {}
        for line in result.stdout.splitlines():
            if line.strip():
                name, _, status = line.partition(",")
                states[name] = status
        return states


class Container:
    """A single LXD instance, addressed by name within an Lxd connection."""

    def __init__(self, lxd: Lxd, name: str) -> None:
        self.lxd = lxd
        self.name = name
        # Cached result of a single `lxc query` for this instance (status +
        # config + devices in one round-trip). _snapshot_loaded distinguishes
        # "not fetched yet" from "fetched, instance absent" (None). Mutating
        # methods call _invalidate() so the next read re-fetches the truth.
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_loaded = False

    def _argv(self, args: list[str]) -> list[str]:
        return self.lxd.argv(args)

    # -- existence / status / lifecycle --

    def snapshot(self, *, refresh: bool = False) -> dict[str, Any] | None:
        """Return this instance's full state (one cached `lxc query`).

        The parsed /1.0/instances/<name> object carries status, config and
        devices together, so existence/status/get_config/devices all read from
        it instead of each spawning their own `lxc` process. Returns None if
        the instance does not exist. Cached for the life of this handle; pass
        refresh=True (or rely on _invalidate() after a mutation) to re-fetch.
        """
        if self._snapshot_loaded and not refresh:
            return self._snapshot
        # `lxc query` ignores the global --project flag (so don't use _argv);
        # the project must be passed as a URL query parameter instead.
        path = f"/1.0/instances/{self.name}?project={self.lxd.project}"
        r = subprocess.run(
            ["lxc", "query", path],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            self._snapshot = json.loads(r.stdout)
        else:
            self._snapshot = None
        self._snapshot_loaded = True
        return self._snapshot

    def _invalidate(self) -> None:
        """Drop the cached snapshot after a state-changing operation."""
        self._snapshot = None
        self._snapshot_loaded = False

    def exists(self) -> bool:
        return self.snapshot() is not None

    def status(self) -> str:
        """Return the instance status (e.g. RUNNING), or "" if it is absent.

        LXD's API spells the status "Running"/"Stopped"; uppercase it to match
        the `lxc list` CSV form callers compare against (== "RUNNING").
        """
        snap = self.snapshot()
        return str(snap.get("status", "")).upper() if snap else ""

    def create(self, image: str) -> None:
        run(self._argv(["init", image, self.name]))
        self._invalidate()

    def clone_from(self, base: Container) -> None:
        run(self._argv(["copy", base.name, self.name]))
        self._invalidate()

    def start(self) -> None:
        run(self._argv(["start", self.name]))
        self._invalidate()

    def stop(self, timeout: int | None = None) -> None:
        """Stop the container, optionally falling back to a forced stop.

        With a timeout, a clean shutdown that doesn't finish in time is
        followed by a forced stop. Session containers are disposable — their
        valuable state lives in bind mounts on the host — so callers that
        stop them prefer a bounded wait over hanging on a wedged guest.
        """
        self._invalidate()
        if timeout is None:
            run(self._argv(["stop", self.name]))
            return
        r = subprocess.run(self._argv(["stop", self.name, f"--timeout={timeout}"]))
        if r.returncode != 0:
            print(
                f"Clean shutdown timed out; force-stopping '{self.name}' ...",
                file=sys.stderr,
            )
            run(self._argv(["stop", self.name, "--force"]))

    def delete(self) -> None:
        run(self._argv(["delete", "--force", self.name]))
        self._invalidate()

    def rename(self, new_name: str) -> "Container":
        """Rename the container; return a new Container handle with the new name.

        The container must be stopped. Used to promote a -provisioning
        temporary container to its final name once provisioning succeeds.
        """
        run(self._argv(["rename", self.name, new_name]))
        self._invalidate()
        return Container(self.lxd, new_name)

    def set_config(self, key: str, value: str) -> None:
        # Skip the write (and its lxc round-trip) when the value is already
        # set — common on warm restarts where limits/base have not changed.
        if self.get_config(key) == value:
            return
        run(self._argv(["config", "set", self.name, key, value]))
        # Keep the cached snapshot coherent without a re-query, so a following
        # set_config (e.g. apply_limits' second key) still reads from cache.
        if self._snapshot is not None:
            self._snapshot.setdefault("config", {})[key] = value

    def apply_limits(self, cpu: int, memory: str) -> None:
        """Apply CPU and memory limits to a running or stopped container.

        Both take effect immediately on a running container.
        """
        self.set_config("limits.cpu", str(cpu))
        self.set_config("limits.memory", memory)

    def get_config(self, key: str) -> str:
        """Return a config key's value, or "" if it is unset."""
        snap = self.snapshot()
        if not snap:
            return ""
        return str(snap.get("config", {}).get(key, ""))

    def ensure_started(self, base: Container) -> None:
        """Clone from a base container if missing, then ensure it's running."""
        if not self.exists():
            print(
                f"Creating session container '{self.name}' from base ...",
                file=sys.stderr,
            )
            self.clone_from(base)
            self.start()
        elif self.status() != "RUNNING":
            print(f"Starting container '{self.name}' ...", file=sys.stderr)
            self.start()

    # -- exec --

    def exec(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Run a command in the container (checked, non-interactive)."""
        return run(self._argv(["exec", self.name, "--"] + list(cmd)), **kwargs)

    def run_interactive(
        self,
        cmd: list[str],
        *,
        cwd: str,
        user: int,
        group: int,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run a command attached to the current terminal; return exit code.

        Unlike exec(), this inherits stdio so the agent gets a real interactive
        terminal, and it does not raise on a non-zero exit.
        """
        argv = self._argv(
            [
                "exec",
                self.name,
                f"--cwd={cwd}",
                f"--user={user}",
                f"--group={group}",
            ]
        )
        for key, value in (env or {}).items():
            argv.append(f"--env={key}={value}")
        return subprocess.run(argv + ["--"] + list(cmd)).returncode

    # -- devices (mounts) --

    def devices(self) -> dict[str, dict[str, str]]:
        """Return the container's instance-local devices dict.

        Served from the cached snapshot (see snapshot()), so the many callers
        that check devices before deciding whether to mutate share a single
        `lxc query` instead of each running their own `config show`.
        """
        snap = self.snapshot()
        return snap.get("devices", {}) if snap else {}

    def _device_paths(self) -> set[str]:
        """Return the set of container paths already occupied by disk devices."""
        return {
            dev["path"]
            for dev in self.devices().values()
            if dev.get("type") == "disk" and "path" in dev
        }

    def add_device(
        self,
        host_path: StrPath,
        work_prefix: str | None = None,
        readonly: bool = False,
    ) -> str:
        # The source is handed to lxc and compared against the strings it
        # reports in `config show`, so normalise to a plain string up front.
        host_path = os.fspath(host_path)
        name = _device_name(host_path)

        # If the device is already configured for this host path, reuse its
        # existing container path. Computing a fresh path here would see the
        # device's own mount as "occupied" and bump to a non-existent suffixed
        # path (e.g. /work/foo-2), leaving the caller with a bad cwd. Reconcile
        # the readonly flag in place if it changed (e.g. re-mounting --ro/--rw).
        devices = self.devices()
        existing = devices.get(name)
        if existing and existing.get("source") == host_path:
            container_path = existing["path"]
            current_ro = str(existing.get("readonly", "false")).lower() == "true"
            if current_ro != readonly:
                run(
                    self._argv(
                        [
                            "config",
                            "device",
                            "set",
                            self.name,
                            name,
                            "readonly",
                            "true" if readonly else "false",
                        ]
                    ),
                    stdout=subprocess.DEVNULL,
                )
                self._invalidate()
                mode = "read-only" if readonly else "read-write"
                print(
                    f"Set {host_path} -> container:{container_path} to {mode}",
                    file=sys.stderr,
                )
            return container_path

        if work_prefix is None:
            container_path = host_path
        else:
            base = Path(host_path).name
            candidate = f"{work_prefix}/{base}"
            occupied = self._device_paths()
            if candidate in occupied:
                suffix = 2
                while f"{candidate}-{suffix}" in occupied:
                    suffix += 1
                candidate = f"{candidate}-{suffix}"
            container_path = candidate

        # Remove any leftover device from a previous crashed session, then add.
        subprocess.run(
            self._argv(["config", "device", "remove", self.name, name]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        add_cmd = [
            "config",
            "device",
            "add",
            self.name,
            name,
            "disk",
            f"source={host_path}",
            f"path={container_path}",
        ]
        if readonly:
            add_cmd.append("readonly=true")
        run(self._argv(add_cmd), stdout=subprocess.DEVNULL)
        self._invalidate()
        mode = " (read-only)" if readonly else ""
        print(
            f"Mounted {host_path} -> container:{container_path}{mode}",
            file=sys.stderr,
        )
        return container_path

    def remove_dir_device(self, host_path: StrPath) -> bool:
        """Remove the dir-* mount for host_path, if present.

        Returns True if a device was removed, False if there was nothing
        mounted for that path. Used by `aiab unmount`.
        """
        name = _device_name(host_path)
        return self.remove_device(name)

    def remove_device(self, name: str) -> bool:
        """Remove a named device, if present. Return True if it was removed."""
        if name not in self.devices():
            return False
        run(
            self._argv(["config", "device", "remove", self.name, name]),
            stdout=subprocess.DEVNULL,
        )
        self._invalidate()
        return True

    # -- network restriction --

    def mask_profile_devices(self, names: list[str]) -> None:
        """Mask profile-inherited devices with container-local 'none' devices.

        Used to detach the NIC(s) the default profile provides, cutting off
        all direct network egress. A 'none' device with the same name as a
        profile device hides it from the container.
        """
        devices = self.devices()
        for name in names:
            if devices.get(name, {}).get("type") == "none":
                continue
            run(
                self._argv(["config", "device", "add", self.name, name, "none"]),
                stdout=subprocess.DEVNULL,
            )
            self._invalidate()
            print(f"Masked network device '{name}'", file=sys.stderr)

    def unmask_profile_devices(self, names: list[str]) -> None:
        """Undo mask_profile_devices: drop the 'none' overrides, if present."""
        devices = self.devices()
        for name in names:
            if devices.get(name, {}).get("type") == "none":
                run(
                    self._argv(["config", "device", "remove", self.name, name]),
                    stdout=subprocess.DEVNULL,
                )
                self._invalidate()
                print(f"Unmasked network device '{name}'", file=sys.stderr)

    def init_pid(self) -> int | None:
        """Return the container's init PID, or None if stopped or not found."""
        try:
            result = run(
                self._argv(["info", self.name]),
                capture_output=True,
                text=True,
            )
            data = yaml.safe_load(result.stdout)
            pid = data.get("PID")
            return int(pid) if pid else None
        except (subprocess.CalledProcessError, ValueError, TypeError, OSError):
            return None

    def add_proxy_device(
        self, name: str, listen: str, connect: str, bind: str = "instance"
    ) -> None:
        """Add (or refresh) a proxy device.

        bind="instance" (default): `listen` is inside the container, `connect`
        is on the host — used to expose the host-side filtering proxy at a
        fixed port inside the container.

        bind="host": `listen` is on the host, `connect` is inside the
        container — used to forward a host port into a container port so the
        host browser can reach a server the agent started.
        """
        existing = self.devices().get(name)
        if (
            existing
            and existing.get("listen") == listen
            and existing.get("connect") == connect
            and existing.get("bind", "instance") == bind
        ):
            return
        self.remove_device(name)
        run(
            self._argv(
                [
                    "config",
                    "device",
                    "add",
                    self.name,
                    name,
                    "proxy",
                    f"listen={listen}",
                    f"connect={connect}",
                    f"bind={bind}",
                ]
            ),
            stdout=subprocess.DEVNULL,
        )
        self._invalidate()

    def add_config_dir(self, source: StrPath, container_path: str, name: str) -> None:
        """Add a named config disk device (e.g. the agent's persistent home)."""
        run(
            self._argv(
                [
                    "config",
                    "device",
                    "add",
                    self.name,
                    name,
                    "disk",
                    f"source={source}",
                    f"path={container_path}",
                ]
            ),
            stdout=subprocess.DEVNULL,
        )
        self._invalidate()

    def set_device_source(self, device_name: str, source: StrPath) -> None:
        """Repoint an existing disk device at a new host source."""
        run(
            self._argv(
                [
                    "config",
                    "device",
                    "set",
                    self.name,
                    device_name,
                    f"source={source}",
                ]
            ),
            stdout=subprocess.DEVNULL,
        )
        self._invalidate()

    def add_config_overlay(
        self,
        host_path: StrPath,
        container_path: str,
        container_user: int = 0,
        readonly: bool = False,
    ) -> None:
        """Bind-mount a host file or directory at an explicit container path.

        Used to overlay versioned config (e.g. CLAUDE.md, commands/) onto the
        container's home, independent of the working-directory mounts, and to
        shadow the repo's .git/hooks and .git/config with per-directory
        sidecars (see aiab.cli's git guard). The device name is derived from
        container_path so the mount is idempotent across sessions, and uses a
        'cfg-' prefix to keep it distinct from the dir-* working mounts. With
        readonly=True the mount is read-only in the container.
        """
        # host_path is handed to lxc / compared against config-show output; the
        # container_path is a path *inside* the container (always POSIX).
        host_path = os.fspath(host_path)
        name = "cfg-" + hashlib.md5(container_path.encode()).hexdigest()[:8]
        devices = self.devices()
        existing = devices.get(name)
        if (
            existing
            and existing.get("source") == host_path
            and existing.get("path") == container_path
            and (str(existing.get("readonly", "false")).lower() == "true") == readonly
        ):
            return
        # Create the mountpoint's parent dirs as container_user first. Otherwise
        # LXD creates any missing parents as container root, which falls outside
        # the idmap and shows up unreadable on the host.
        parent = str(PurePosixPath(container_path).parent)
        if parent and parent != "/" and container_user != 0:
            subprocess.run(
                self._argv(
                    [
                        "exec",
                        self.name,
                        f"--user={container_user}",
                        f"--group={container_user}",
                        "--",
                        "mkdir",
                        "-p",
                        parent,
                    ]
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            self._argv(["config", "device", "remove", self.name, name]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        add_cmd = [
            "config",
            "device",
            "add",
            self.name,
            name,
            "disk",
            f"source={host_path}",
            f"path={container_path}",
        ]
        if readonly:
            add_cmd.append("readonly=true")
        run(self._argv(add_cmd), stdout=subprocess.DEVNULL)
        self._invalidate()
        mode = " (read-only)" if readonly else ""
        print(
            f"Overlaid {host_path} -> container:{container_path}{mode}",
            file=sys.stderr,
        )

    def mount_wayland(self, container_user: int) -> dict[str, str]:
        """Bind-mount the host Wayland socket; return env vars to set (or {}).

        Reads WAYLAND_DISPLAY and XDG_RUNTIME_DIR from the host environment and
        mirrors the socket at the same path inside the container, so the
        returned WAYLAND_DISPLAY/XDG_RUNTIME_DIR need no adjustment. Returns an
        empty dict if the host environment is not set up for Wayland.
        """
        xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
        wayland_display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not xdg_runtime_dir:
            print(
                "Warning: XDG_RUNTIME_DIR not set; skipping Wayland passthrough",
                file=sys.stderr,
            )
            return {}

        socket_path = Path(xdg_runtime_dir) / wayland_display
        if not socket_path.exists():
            print(
                f"Warning: Wayland socket {socket_path} not found; "
                "skipping Wayland passthrough",
                file=sys.stderr,
            )
            return {}
        # Handed to lxc and mirrored at the same path inside the container.
        socket_host = str(socket_path)
        socket_container = socket_host

        name = "wayland-" + hashlib.md5(socket_host.encode()).hexdigest()[:8]

        # Always recreate the directory and file mountpoint inside the
        # container. /run is a fresh tmpfs on every container start, so both
        # disappear after each restart. A file mountpoint must exist for LXD
        # to bind-mount a socket (a directory mountpoint is not enough).
        subprocess.run(
            self._argv(
                [
                    "exec",
                    self.name,
                    "--",
                    "bash",
                    "-c",
                    f"mkdir -p {xdg_runtime_dir} && "
                    f"chown {container_user}:{container_user} {xdg_runtime_dir} && "
                    f"chmod 700 {xdg_runtime_dir} && "
                    f"touch {socket_container}",
                ]
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Always remove-then-add the device so LXD performs a fresh live
        # bind-mount now that the mountpoint exists. Without this, LXD already
        # attempted (and silently failed) the mount at container-start time
        # when /run was still empty, and will not retry.
        subprocess.run(
            self._argv(["config", "device", "remove", self.name, name]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run(
            self._argv(
                [
                    "config",
                    "device",
                    "add",
                    self.name,
                    name,
                    "disk",
                    f"source={socket_host}",
                    f"path={socket_container}",
                ]
            ),
            stdout=subprocess.DEVNULL,
        )
        self._invalidate()

        return {
            "WAYLAND_DISPLAY": wayland_display,
            "XDG_RUNTIME_DIR": xdg_runtime_dir,
        }
