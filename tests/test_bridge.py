# Tests for aiab.bridge and its container side, bridge_client.py. Run end to
# end: a real server on a real socket, the real client run as wl-copy and
# wl-paste, and stand-in wl-clipboard commands on the host's PATH that record
# what they were asked.

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import aiab.bridge as bridge
import aiab.state as state

CLIENT = Path(bridge.__file__).with_name("bridge_client.py")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_DIRSTATE_DIR", tmp_path / "dirstate")


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Stand-in wl-copy/wl-paste on PATH, and a live Wayland session."""
    bin_dir = tmp_path / "hostbin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    clip = tmp_path / "clip"
    clip.write_bytes(b"hello\n")
    scripts = {
        # Prints the clipboard, or a type list; fails on an unknown type the
        # way the real one does.
        "wl-paste": f"""
echo "wl-paste $*" >> {log}
case "$*" in
  *--list-types*) printf 'text/plain\\nimage/png\\n' ;;
  *image/gif*) echo "No suitable type of content copied" >&2; exit 1 ;;
  *) cat {clip} ;;
esac
""",
        # Stores stdin, then leaves a child behind holding stdout and stderr
        # open — which the real one does, to serve the selection.
        "wl-copy": f"""
echo "wl-copy $*" >> {log}
cat > {clip}
sleep 30 &
""",
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    return log, clip


@pytest.fixture
def server(tmp_path):
    d = tmp_path / "bridge"
    s = bridge.Server(d, str(d))
    s.start()
    yield s
    s.close()


@pytest.fixture
def client(tmp_path, server):
    """Run the client as one of its commands, pointed at the server."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    for shim in bridge.SHIMS:
        (shim_dir / shim).symlink_to(CLIENT)

    def run(cmd, *args, input=b"", env=None):
        e = {"PATH": os.environ["PATH"], bridge.ENV_VAR: server.container_path}
        e.update(env or {})
        return subprocess.run(
            [sys.executable, "-I", str(shim_dir / cmd), *args],
            input=input,
            capture_output=True,
            env=e,
            timeout=20,
        )

    return run


def _calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_paste_returns_the_host_clipboard(host, client):
    log, _ = host
    r = client("wl-paste")
    assert (r.returncode, r.stdout) == (0, b"hello\n")
    assert _calls(log) == ["wl-paste "]


def test_paste_passes_its_options_through(host, client):
    log, _ = host
    r = client("wl-paste", "--type", "image/png", "-n", "-p")
    assert r.returncode == 0
    assert _calls(log) == ["wl-paste --primary --no-newline --type image/png"]


def test_listing_types(host, client):
    r = client("wl-paste", "-l")
    assert r.stdout == b"text/plain\nimage/png\n"


def test_a_host_failure_is_the_clients_failure(host, client):
    # Claude Code checks for an image with `wl-paste --type image/... || ...`,
    # so a missing type has to come back as a non-zero exit, not empty output.
    r = client("wl-paste", "--type", "image/gif")
    assert r.returncode == 1
    assert r.stdout == b""
    assert b"No suitable type" in r.stderr


def test_copy_from_stdin(host, client):
    log, clip = host
    start = time.monotonic()
    r = client("wl-copy", input=b"copied")
    # The left-behind child holds wl-copy's stdout and stderr: if either were
    # a pipe the server waited on, this would take the child's 30 seconds.
    assert time.monotonic() - start < 10
    assert r.returncode == 0, r.stderr
    assert clip.read_bytes() == b"copied"
    assert _calls(log) == ["wl-copy "]


def test_copy_from_arguments_and_primary(host, client):
    log, clip = host
    r = client("wl-copy", "--primary", "two", "words")
    assert r.returncode == 0
    assert clip.read_bytes() == b"two words"
    assert _calls(log) == ["wl-copy --primary"]


def test_binary_data_survives_the_trip(host, client):
    _, clip = host
    data = bytes(range(256)) * 4096  # a megabyte, newlines and all
    assert client("wl-copy", "-t", "image/png", input=data).returncode == 0
    assert clip.read_bytes() == data
    assert client("wl-paste", "-t", "image/png").stdout == data


def test_without_wayland_on_the_host_it_says_so(host, client, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY")
    r = client("wl-paste")
    assert r.returncode == 1
    assert b"no Wayland session" in r.stderr


def test_without_a_bridge_it_says_so(host, client):
    r = client("wl-paste", env={bridge.ENV_VAR: ""})
    assert r.returncode == 1
    assert b"AIAB_BRIDGE unset" in r.stderr


def test_an_unsupported_option_is_refused(host, client):
    # --watch would need a compositor of our own; better loud than ignored.
    r = client("wl-paste", "--watch", "true")
    assert r.returncode == 2
    assert _calls(host[0]) == []


def _raw(server, payload):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(server.container_path)
    s.sendall(payload)
    s.shutdown(socket.SHUT_WR)
    reply = b""
    while chunk := s.recv(65536):
        reply += chunk
    s.close()
    line, _, data = reply.partition(b"\n")
    return json.loads(line), data


def test_only_listed_operations_are_served(server):
    header, _ = _raw(server, b'{"op": "shell.run", "args": {}}\n')
    assert header == {"ok": False, "error": "unknown operation 'shell.run'"}


@pytest.mark.parametrize("payload", [b"not json\n", b"[1]\n", b"no newline"])
def test_garbage_is_refused(server, payload):
    header, _ = _raw(server, payload)
    assert header == {"ok": False, "error": "malformed request"}


def test_type_must_be_a_string(host, server):
    header, _ = _raw(server, b'{"op": "clipboard.paste", "args": {"type": 1}}\n')
    assert header["ok"] is False


def test_close_removes_the_socket(tmp_path):
    d = tmp_path / "b"
    s = bridge.Server(d, str(d))
    s.start()
    assert (d / s.name).exists()
    s.close()
    assert not (d / s.name).exists()


def test_a_dead_runs_socket_is_reaped_and_a_live_one_kept(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(d / "1.sock"))
    dead.close()  # the file stays; nothing listens on it
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(d / "2.sock"))
    live.listen()
    s = bridge.Server(d, str(d))
    s.start()
    try:
        assert sorted(os.listdir(d)) == sorted(["2.sock", s.name])
    finally:
        s.close()
        live.close()


def test_a_path_too_long_for_sockaddr_un_still_binds(tmp_path):
    d = tmp_path / ("x" * 80) / ("y" * 80)
    s = bridge.Server(d, str(d))
    s.start()
    try:
        assert (d / s.name).exists()
    finally:
        s.close()


def test_the_directory_is_private(tmp_path):
    with bridge.serve(tmp_path / "project") as env:
        path = env[bridge.ENV_VAR]
        assert path.startswith("/aiab/bridge/")
        host_dir = state.dir_state_path(tmp_path / "project") / bridge.SUBDIR
        assert (host_dir.stat().st_mode & 0o777) == 0o700
        assert (host_dir / Path(path).name).exists()


def test_install_writes_the_client_and_links_both_commands():
    class _Container:
        def exec(self, cmd, **kwargs):
            self.cmd, self.input = cmd, kwargs["input"]

    c = _Container()
    bridge.install(c)  # type: ignore[arg-type]
    assert c.cmd[:2] == ["sh", "-c"]
    assert c.input == CLIENT.read_bytes()
    script = c.cmd[2]
    for shim in bridge.SHIMS:
        assert f"ln -sf {bridge.CLIENT_PATH} /usr/local/bin/{shim}" in script


def test_install_script_runs(tmp_path, monkeypatch):
    # Run the generated shell for real, rooted in a scratch directory.
    root = tmp_path / "root"
    monkeypatch.setattr(
        bridge, "CLIENT_PATH", f"{root}/usr/local/lib/aiab/bridge-client"
    )
    monkeypatch.setattr(bridge, "_SHIM_DIR", f"{root}/usr/local/bin")
    (root / "usr/local/bin").mkdir(parents=True)
    subprocess.run(
        ["sh", "-c", bridge.install_script()], input=b"#!/bin/sh\n", check=True
    )
    client = Path(bridge.CLIENT_PATH)
    assert client.read_bytes() == b"#!/bin/sh\n"
    assert os.access(client, os.X_OK)
    for shim in bridge.SHIMS:
        assert os.readlink(root / "usr/local/bin" / shim) == str(client)
