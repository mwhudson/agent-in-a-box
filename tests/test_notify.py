# Tests for aiab.notify — the desktop notifications `aiab monitor` raises for
# parked hosts. There is no notification daemon (or session bus) in a test
# environment, so these put a stub `notify-send` on PATH that speaks the same
# protocol: print the id, then print an action name when one is "clicked".

import subprocess
import threading
import time

import pytest

import aiab.notify as notify


def _fake_notify_send(tmp_path, name, script):
    """Write an executable stub and return a PATH that finds it first."""
    bindir = tmp_path / f"bin-{name}"
    bindir.mkdir(exist_ok=True)
    path = bindir / "notify-send"
    path.write_text(script)
    path.chmod(0o755)
    return str(bindir)


# Prints the id, then blocks until told to answer via a file: that lets a test
# hold a notification open and check what happens while it is up.
_WAITING = """#!/bin/sh
echo 41
echo "$@" > {argv}
while [ ! -f {answer} ]; do sleep 0.02; done
cat {answer}
"""

# Answers immediately, as if the user clicked Allow the moment it appeared.
_CLICKS = """#!/bin/sh
echo 41
echo allow
"""


def _wait_until(predicate):
    """Spin until the stub has got far enough, rather than hang if it never does."""
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "the stub notify-send never ran"
        time.sleep(0.01)


class _Clicks:
    """Collect (key, action) callbacks, with a way to wait for the first.

    They arrive on the notifier's reader thread, so a test that expects one
    waits on the event rather than sleeping and hoping.
    """

    def __init__(self):
        self.seen = []
        self.arrived = threading.Event()

    def __call__(self, key, action):
        self.seen.append((key, action))
        self.arrived.set()


@pytest.fixture
def clicked():
    return _Clicks()


def test_disabled_without_notify_send(monkeypatch, clicked, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    notifier = notify.Notifier(clicked)
    assert not notifier.enabled
    # Every operation is a no-op rather than an error.
    notifier.notify("example.com", "summary", "body", [("allow", "Allow")])
    notifier.close("example.com")
    notifier.close_all()
    assert clicked.seen == []


def test_click_reports_the_action_for_its_key(monkeypatch, clicked, tmp_path):
    monkeypatch.setenv("PATH", _fake_notify_send(tmp_path, "click", _CLICKS))
    notifier = notify.Notifier(clicked)
    notifier.notify("example.com", "summary", "body", [("allow", "Allow")])
    assert clicked.arrived.wait(5)
    assert clicked.seen == [("example.com", "allow")]


def test_actions_become_notify_send_buttons(monkeypatch, clicked, tmp_path):
    argv = tmp_path / "argv"
    answer = tmp_path / "answer"
    script = _WAITING.format(argv=argv, answer=answer)
    monkeypatch.setenv("PATH", _fake_notify_send(tmp_path, "argv", script))
    notifier = notify.Notifier(clicked)
    notifier.notify(
        "example.com", "summary", "body", [("allow", "Allow"), ("deny", "Deny")]
    )
    _wait_until(argv.exists)  # the stub writes it before it starts waiting
    words = argv.read_text().split()
    assert "--action" in words
    assert "allow=Allow" in words
    assert "deny=Deny" in words
    assert words[-2:] == ["summary", "body"]
    notifier.close_all()


def test_one_notification_per_key(monkeypatch, clicked, tmp_path):
    answer = tmp_path / "answer"
    script = _WAITING.format(argv=tmp_path / "argv", answer=answer)
    monkeypatch.setenv("PATH", _fake_notify_send(tmp_path, "dedupe", script))
    notifier = notify.Notifier(clicked)
    for _ in range(3):
        notifier.notify("example.com", "summary", "body", [("allow", "Allow")])
    assert len(notifier._live) == 1
    notifier.close_all()


def test_close_withdraws_and_suppresses_a_late_click(monkeypatch, clicked, tmp_path):
    answer = tmp_path / "answer"
    script = _WAITING.format(argv=tmp_path / "argv", answer=answer)
    monkeypatch.setenv("PATH", _fake_notify_send(tmp_path, "close", script))
    notifier = notify.Notifier(clicked)
    notifier.notify("example.com", "summary", "body", [("allow", "Allow")])
    (live,) = notifier._live.values()

    notifier.close("example.com")  # decided in the pane instead
    live.proc.wait(5)
    assert notifier._live == {}

    # A verdict the stub was about to print is dropped: the question was
    # already answered, and a racing click must not overrule that.
    answer.write_text("allow\n")
    assert not clicked.arrived.wait(0.5)
    assert clicked.seen == []


def test_close_asks_the_daemon_to_withdraw_by_id(monkeypatch, clicked, tmp_path):
    answer = tmp_path / "answer"
    script = _WAITING.format(argv=tmp_path / "argv", answer=answer)
    monkeypatch.setenv("PATH", _fake_notify_send(tmp_path, "gdbus", script))
    notifier = notify.Notifier(clicked)
    notifier._gdbus = "/usr/bin/gdbus"  # not run: subprocess.run is stubbed
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    notifier.notify("example.com", "summary", "body", [("allow", "Allow")])
    (live,) = notifier._live.values()
    # The stub prints the id before it starts waiting, so it lands in time.
    _wait_until(lambda: live.ident is not None)
    notifier.close("example.com")

    (cmd,) = calls
    assert cmd[0] == "/usr/bin/gdbus"
    assert cmd[-2] == "org.freedesktop.Notifications.CloseNotification"
    assert cmd[-1] == "41"


def test_bus_address_falls_back_to_the_socket_path(monkeypatch, tmp_path):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    socket = tmp_path / "bus"
    socket.write_text("")
    monkeypatch.setattr(notify, "_BUS_SOCKET", socket)
    assert notify._bus_env()["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={socket}"


def test_inherited_bus_address_is_kept(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    env = notify._bus_env()
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
