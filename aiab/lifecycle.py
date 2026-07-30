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
# aiab.lifecycle - session lifecycle plumbing: the filtering proxy and the
# delayed idle-stop mechanism.
#
# Both aiab.cli (which starts sessions and proxies) and aiab.stopper (the
# detached helper that stops an idle one later) need this, so it lives here
# rather than in cli — importing cli pulls in click and the whole command
# tree, which is wasteful for a tiny detached process. Named 'lifecycle'
# rather than 'session' because cli has local variables named `session`
# (an lxd.Container) everywhere; a same-named import would be confusing at
# every call site.

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import click

from . import agents, lxd, netproxy, netwatch

# Per-container lock files live here. Each 'aiab run' holds a shared flock
# for the duration of its session; when the last one exits, a detached helper
# (aiab.stopper) stops the container after IDLE_STOP_DELAY unless a new
# session has taken the lock again — so exiting doesn't block on the stop,
# and back-to-back sessions reuse the still-running container.
LOCK_DIR: Path = Path.home() / ".local" / "share" / "aiab" / "locks"

# How long a container stays up after its last session exits (seconds).
IDLE_STOP_DELAY: float = 5 * 60

# The stopper helpers log here, named after the container.
STOPPER_DIR: Path = Path.home() / ".local" / "share" / "aiab" / "stopper"


def helper_env() -> dict[str, str]:
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


def proxy_socket_name(container_name: str) -> str:
    """The abstract socket address for a container's proxy, with leading @.

    Understood in this form by both aiab.netproxy (--socket) and LXD proxy
    devices (connect=unix:@...). Includes the uid so concurrent aiab users on
    one host can't collide in the abstract namespace.
    """
    return f"@aiab-{os.getuid()}-{container_name}"


def proxy_pid(container_name: str) -> int | None:
    """Return the pid of a live proxy for a container, or None."""
    try:
        pid = int((netproxy.PROXY_DIR / f"{container_name}.pid").read_text())
        os.kill(pid, 0)  # just probes for existence
    except (OSError, ValueError):
        return None
    return pid


def proxy_socket_live(socket_name: str) -> bool:
    """Return True if something accepts connections on an @abstract socket."""
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect("\0" + socket_name[1:])
    except OSError:
        return False
    finally:
        s.close()
    return True


def ensure_proxy(
    session: lxd.Container, work_dir: Path, agent: str, profile: str | None = None
) -> str:
    """Start the filtering proxy for a session container (or reuse a live one).

    Returns the abstract socket address (with leading @) the proxy listens
    on. The proxy is shared by concurrent `aiab run`s for the same container
    and stopped alongside the container by stop_when_idle.
    """
    netproxy.PROXY_DIR.mkdir(parents=True, exist_ok=True)
    sock_name = proxy_socket_name(session.name)
    log = netproxy.PROXY_DIR / f"{session.name}.log"
    if proxy_pid(session.name) is not None and proxy_socket_live(sock_name):
        return sock_name

    argv = [
        sys.executable,
        "-m",
        "aiab.netproxy",
        f"--socket={sock_name}",
        f"--dir={work_dir}",
        f"--agent={agent}",
        f"--pending-dir={netwatch.pending_dir(work_dir)}",
    ]
    if profile:
        argv.append(f"--profile={profile}")
    with log.open("ab") as log_fd:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            env=helper_env(),
        )
    (netproxy.PROXY_DIR / f"{session.name}.pid").write_text(f"{proc.pid}\n")

    # Wait for the socket so the LXD proxy device has something to connect to.
    for _ in range(50):
        if proxy_socket_live(sock_name):
            break
        if proc.poll() is not None:
            raise click.ClickException(f"network proxy failed to start; see {log}")
        time.sleep(0.1)
    else:
        raise click.ClickException(f"network proxy did not come up; see {log}")
    print(f"Started filtering proxy (denials logged to {log})", file=sys.stderr)
    return sock_name


def stop_proxy(container_name: str) -> None:
    """Stop the proxy for a container, if one is running, and clean up.

    The abstract socket disappears with the process; the .sock unlink only
    cleans up files left by versions that used filesystem sockets.
    """
    pid = proxy_pid(container_name)
    if pid is not None:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    (netproxy.PROXY_DIR / f"{container_name}.pid").unlink(missing_ok=True)
    (netproxy.PROXY_DIR / f"{container_name}.sock").unlink(missing_ok=True)


def spawn_stopper(container_name: str) -> None:
    """Launch the detached helper that stops an idle container later."""
    STOPPER_DIR.mkdir(parents=True, exist_ok=True)
    log = STOPPER_DIR / f"{container_name}.log"
    with log.open("ab") as log_fd:
        subprocess.Popen(
            [sys.executable, "-m", "aiab.stopper", container_name],
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            env=helper_env(),
        )


def session_in_use(container_name: str) -> bool:
    """Return True if another `aiab run` is currently using this container.

    Probes the same per-container lock stop_when_idle() takes: a live session
    holds it *shared* for its whole duration, so a non-blocking *exclusive*
    attempt fails exactly while one is running. Closing the fd releases
    anything the probe itself acquired, so this never holds a lock and never
    blocks a real session from taking one.

    Only meaningful before this process enters stop_when_idle() — after that
    we hold the lock ourselves and would be reporting on us.
    """
    lock_path = LOCK_DIR / container_name
    try:
        # Not "w": truncating is pointless here (the contents are unused) and
        # this must not create the file, or a first-ever run would leave a
        # stray lock file behind for a container that may never be built.
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False  # no lock file, so nothing has ever locked it
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


@contextlib.contextmanager
def stop_when_idle(session: lxd.Container) -> Iterator[None]:
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
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with (LOCK_DIR / session.name).open("w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        try:
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass  # another aiab process is still using this container
            else:
                spawn_stopper(session.name)
                print(
                    f"Container '{session.name}' stops in "
                    f"{int(IDLE_STOP_DELAY // 60)} minutes unless a new "
                    "session starts.",
                    file=sys.stderr,
                )
