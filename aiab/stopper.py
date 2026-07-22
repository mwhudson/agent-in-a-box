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
# aiab.stopper - delayed stop for idle session containers.
#
# Spawned detached by `aiab run` when the last session using a container
# exits (see cli). Stopping the container right there would make every exit
# block for a few seconds, and back-to-back sessions would each pay a fresh
# container start; instead this helper sleeps, then takes the per-container
# lock exclusively. If that fails, a new session has started in the meantime
# and the container stays up; if it succeeds, nothing is using the container
# and it is torn down exactly as an immediate stop would have.

from __future__ import annotations

import argparse
import fcntl
import time

from . import PROJECT
from . import lifecycle
from . import lxd


def _log(message: str) -> None:
    # stdout is a per-container logfile (see lifecycle.spawn_stopper).
    print(f"{time.strftime('%F %T')} {message}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container", help="session container name")
    parser.add_argument(
        "--delay",
        type=float,
        default=lifecycle.IDLE_STOP_DELAY,
        help="seconds to wait before stopping (default: %(default)s)",
    )
    args = parser.parse_args()

    time.sleep(args.delay)

    lifecycle.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with (lifecycle.LOCK_DIR / args.container).open("w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _log(f"{args.container}: in use again; leaving it running")
            return
        container = lxd.Lxd(PROJECT).container(args.container)
        if not container.exists():
            _log(f"{args.container}: already gone")
            return
        # Same teardown order as cli: kill the host-side proxy and detach its
        # device first, so neither can hold up a clean guest shutdown.
        lifecycle.stop_proxy(args.container)
        container.remove_device("netproxy")
        if container.status() == "RUNNING":
            _log(f"{args.container}: stopping idle container")
            container.stop(timeout=30)
        else:
            _log(f"{args.container}: already stopped")


if __name__ == "__main__":
    main()
