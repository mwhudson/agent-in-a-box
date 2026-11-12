# Tests for aiab.shellwin — finding, or opening, the tmux window holding a
# shell in a session container. tmux itself is stubbed out here: what each
# command and format string does was verified against a real tmux (3.6),
# including that a window id on its own selects in whichever session tmux used
# last (hence the session-qualified targets these check for). What this module
# has to get right is which question it asks, and of which session.

from pathlib import Path

import pytest

import aiab.shellwin as shellwin


def _window(index="1", mark="", window_id=None):
    """One `tmux list-windows` line in the format aiab.shellwin asks for."""
    return f"{window_id or f'@{index}'}\t{index}\t{mark}\n"


def _fake_tmux(monkeypatch, windows=(), *, session="$0", created="@9\t2"):
    """Answer tmux's part of the conversation; return the calls it was asked.

    `windows` is the session's window list, as a list of _window() lines a
    test can rewrite between asks.
    """
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "display-message":
            return f"{session}\n"
        if args[0] == "list-windows":
            return "".join(windows)
        if args[0] == "new-window":
            return f"{created}\n"
        return ""

    monkeypatch.setattr(shellwin, "_tmux", run)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    return calls


def _call(calls, name):
    """The one call to `tmux <name>`, or None."""
    found = [c for c in calls if c[0] == name]
    assert len(found) <= 1, found
    return found[0] if found else None


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


def test_outside_tmux_there_is_no_session(monkeypatch):
    # And tmux is not asked: `display-message` outside tmux answers happily
    # about whichever session the server used last, and acting on that would
    # open a window in a session nobody is looking at.
    calls = _fake_tmux(monkeypatch)
    monkeypatch.delenv("TMUX")
    assert shellwin.session() is None
    assert calls == []


def test_inside_tmux_the_session_is_the_one_tmux_names(monkeypatch):
    _fake_tmux(monkeypatch, session="$3")
    assert shellwin.session() == "$3"


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def test_find_matches_the_container_not_the_window_name(monkeypatch):
    # The mark is the identity; two directories' shells are both called
    # 'shell', and in a session of your own one may be a window you named.
    windows = [
        _window(index="0"),
        _window(index="1", mark="claude-other-999999"),
        _window(index="2", mark="claude-proj-abc123"),
    ]
    _fake_tmux(monkeypatch, windows)
    found = shellwin.find("$0", "claude-proj-abc123")
    assert found is not None
    assert (found.id, found.index, found.created) == ("@2", "2", False)


def test_find_ignores_unmarked_windows(monkeypatch):
    _fake_tmux(monkeypatch, [_window(index="0"), _window(index="1")])
    assert shellwin.find("$0", "claude-proj-abc123") is None


def test_find_asks_about_the_session_it_was_given(monkeypatch):
    calls = _fake_tmux(monkeypatch, [_window()])
    shellwin.find("$7", "claude-proj-abc123")
    assert _call(calls, "list-windows")[:3] == ("list-windows", "-t", "$7")


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


def test_command_runs_a_shell_session_without_a_monitor_pane():
    # --no-tmux is the point: a shell window that split a monitor of its own
    # would leave two monitors answering the same parked host.
    cmd = shellwin.command(Path("/work/proj"), "claude", argv0="/usr/bin/aiab")
    assert cmd == [
        "/usr/bin/aiab",
        "run",
        "--shell",
        "--no-tmux",
        "--for=/work/proj",
        "claude",
    ]


def test_command_carries_the_profile_through():
    # Without it an isolated profile's shell would land in the plain agent's
    # container, with the wrong credentials mounted.
    cmd = shellwin.command(
        Path("/work/proj"), "claude", "openrouter", argv0="/usr/bin/aiab"
    )
    assert "--profile=openrouter" in cmd
    assert cmd[-1] == "claude"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_switches_to_an_existing_shell_window(monkeypatch):
    windows = [_window(index="0"), _window(index="2", mark="claude-proj-abc123")]
    calls = _fake_tmux(monkeypatch, windows, session="$1")
    window = shellwin.show(Path("/work/proj"), "claude-proj-abc123", "claude")

    assert (window.id, window.created) == ("@2", False)
    assert _call(calls, "new-window") is None
    # Qualified with our session: a bare window id would move whichever
    # session tmux used last, which in a session group need not be ours.
    assert _call(calls, "select-window") == ("select-window", "-t", "$1:@2")


def test_show_opens_a_window_when_there_is_none(monkeypatch):
    calls = _fake_tmux(monkeypatch, [_window(index="0")], session="$1")
    window = shellwin.show(Path("/work/proj"), "claude-proj-abc123", "claude")

    assert (window.id, window.index, window.created) == ("@9", "2", True)
    new = _call(calls, "new-window")
    # In our session, at the next free index (the trailing colon), named for
    # the status line, started in the directory it is a shell for.
    assert "-t" in new and new[new.index("-t") + 1] == "$1:"
    assert new[new.index("-n") + 1] == shellwin.WINDOW_NAME
    assert new[new.index("-c") + 1] == "/work/proj"
    # The window's command is the shell session itself, as one shell word.
    assert new[-1].endswith("run --shell --no-tmux --for=/work/proj claude")


def test_show_marks_the_window_with_its_container(monkeypatch):
    # Set here rather than by the shell itself, which would leave the window
    # unmarked for as long as the container takes to start — long enough for
    # a second ask to miss it and open a duplicate.
    calls = _fake_tmux(monkeypatch, [], session="$1")
    shellwin.show(Path("/work/proj"), "claude-proj-abc123", "claude")
    assert _call(calls, "set-option") == (
        "set-option",
        "-w",
        "-t",
        "@9",
        shellwin.MARK,
        "claude-proj-abc123",
    )


def test_show_outside_tmux_says_so(monkeypatch):
    _fake_tmux(monkeypatch, [])
    monkeypatch.delenv("TMUX")
    with pytest.raises(shellwin.Error):
        shellwin.show(Path("/work/proj"), "claude-proj-abc123", "claude")


def test_show_reports_a_window_tmux_would_not_name(monkeypatch):
    # -P should always answer; if it somehow doesn't there is nothing to mark,
    # and an unmarked window would be invisible to every later ask.
    _fake_tmux(monkeypatch, [], created="")
    with pytest.raises(shellwin.Error):
        shellwin.show(Path("/work/proj"), "claude-proj-abc123", "claude")
