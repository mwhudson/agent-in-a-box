# Tests for aiab.shellwin — finding the pane holding a container's shell, and
# moving it between a window of its own and the agent's window. tmux itself is
# stubbed out here: what each command and format string does was verified
# against a real tmux (3.6) — that a pane keeps its user options and its
# process across join-pane/break-pane, and that a window id on its own selects
# in whichever session tmux used last (hence the session-qualified targets
# these check for). What this module has to get right is which question it
# asks, of which session, and which way it moves the shell.

from pathlib import Path

import pytest

import aiab.shellwin as shellwin

CONTAINER = "claude-proj-abc123"


def _pane(pane="%2", window="@2", index="2", panes="1", mark=""):
    """One `tmux list-panes` line in the format aiab.shellwin asks for."""
    return f"{pane}\t{window}\t{index}\t{panes}\t{mark}\n"


def _fake_tmux(
    monkeypatch,
    panes=(),
    *,
    session="$0",
    window="@0",
    created="%9\t2",
    broke="3",
):
    """Answer tmux's part of the conversation; return the calls it was asked.

    `panes` is every pane in the session, as _pane() lines a test can rewrite
    between asks. `window` is the one the caller is looking at.
    """
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "display-message":
            return f"{window}\n" if args[-1] == "#{window_id}" else f"{session}\n"
        if args[0] == "list-panes":
            return "".join(panes)
        if args[0] in ("new-window", "split-window"):
            return f"{created}\n"
        if args[0] == "break-pane":
            return f"{broke}\n"
        return ""

    monkeypatch.setattr(shellwin, "_tmux", run)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    # Set per-test: whether the caller is in a pane of its own changes who
    # tmux answers these questions about.
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return calls


def _call(calls, name):
    """The one call to `tmux <name>`, or None."""
    found = [c for c in calls if c[0] == name]
    assert len(found) <= 1, found
    return found[0] if found else None


def _arg(call, flag):
    """The value given to `flag` in a recorded call."""
    return call[call.index(flag) + 1]


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


def test_outside_tmux_there_is_no_session(monkeypatch):
    # And tmux is not asked: `display-message` outside tmux answers happily
    # about whichever session the server used last, and acting on that would
    # put a shell in a session nobody is looking at.
    calls = _fake_tmux(monkeypatch)
    monkeypatch.delenv("TMUX")
    assert shellwin.session() is None
    assert calls == []


def test_inside_tmux_the_session_is_the_one_tmux_names(monkeypatch):
    _fake_tmux(monkeypatch, session="$3")
    assert shellwin.session() == "$3"


def test_lookups_are_anchored_to_the_callers_own_pane(monkeypatch):
    # Not to the session's current window, which is a different thing: the
    # monitor pane lives in the agent's window and has to put a shell *there*
    # even while the terminal is off looking at another window — the state
    # tmux is left in by opening a shell window a moment earlier.
    calls = _fake_tmux(monkeypatch, [_pane(pane="%0")], session="$1", window="@0")
    monkeypatch.setenv("TMUX_PANE", "%1")
    shellwin.show(Path("/work/proj"), CONTAINER, "claude", as_pane=True)

    asked = [c for c in calls if c[0] == "display-message"]
    assert asked
    assert all(_arg(c, "-t") == "%1" for c in asked)


def test_lookups_follow_the_client_when_there_is_no_pane(monkeypatch):
    # A tmux key binding (`run-shell 'aiab shell'`) runs no pane of its own,
    # but it does have the client whose keyboard was just used, and tmux's
    # unanchored answer is that client's.
    calls = _fake_tmux(monkeypatch, [], session="$1")
    assert shellwin.session() == "$1"
    assert all("-t" not in c for c in calls if c[0] == "display-message")


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def test_find_matches_the_container_not_the_window_name(monkeypatch):
    # The mark is the identity; two directories' shells are both called
    # 'shell', and in a session of your own one may be a window you named.
    panes = [
        _pane(pane="%0", window="@0", index="0", panes="2"),
        _pane(pane="%5", window="@1", index="1", mark="claude-other-999999"),
        _pane(mark=CONTAINER),
    ]
    _fake_tmux(monkeypatch, panes)
    found = shellwin.find("$0", CONTAINER)
    assert found is not None
    assert (found.pane, found.window, found.index) == ("%2", "@2", "2")


def test_find_knows_whether_the_shell_has_its_window_to_itself(monkeypatch):
    # Which decides whether showing it full-window is a select or a break-out.
    _fake_tmux(monkeypatch, [_pane(panes="1", mark=CONTAINER)])
    own_window = shellwin.find("$0", CONTAINER)
    assert own_window is not None and own_window.alone is True

    _fake_tmux(monkeypatch, [_pane(window="@0", panes="3", mark=CONTAINER)])
    joined = shellwin.find("$0", CONTAINER)
    assert joined is not None and joined.alone is False


def test_find_ignores_unmarked_panes(monkeypatch):
    _fake_tmux(monkeypatch, [_pane(pane="%0"), _pane(pane="%1")])
    assert shellwin.find("$0", CONTAINER) is None


def test_find_asks_about_every_pane_in_the_session_it_was_given(monkeypatch):
    # -s rather than the current window: a shell parked in its own window is
    # one this has to see from wherever the ask came from.
    calls = _fake_tmux(monkeypatch, [_pane()])
    shellwin.find("$7", CONTAINER)
    assert _call(calls, "list-panes")[:4] == ("list-panes", "-s", "-t", "$7")


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


def test_command_runs_a_shell_session_without_a_monitor_pane():
    # --no-tmux is the point: a shell that split a monitor of its own would
    # leave two monitors raising a banner each for the same parked host.
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
# show — a window of its own
# ---------------------------------------------------------------------------


def test_show_switches_to_an_existing_shell_window(monkeypatch):
    panes = [_pane(pane="%0", window="@0", index="0", panes="2"), _pane(mark=CONTAINER)]
    calls = _fake_tmux(monkeypatch, panes, session="$1")
    outcome = shellwin.show(Path("/work/proj"), CONTAINER, "claude")

    assert outcome.action == shellwin.SWITCHED
    assert _call(calls, "new-window") is None
    # Qualified with our session: a bare window id would move whichever
    # session tmux used last, which in a session group need not be ours.
    assert _call(calls, "select-window") == ("select-window", "-t", "$1:@2")


def test_show_opens_a_window_when_there_is_none(monkeypatch):
    calls = _fake_tmux(monkeypatch, [_pane(pane="%0")], session="$1")
    outcome = shellwin.show(Path("/work/proj"), CONTAINER, "claude")

    assert (outcome.action, outcome.index) == (shellwin.OPENED, "2")
    new = _call(calls, "new-window")
    # In our session, at the next free index (the trailing colon), named for
    # the status line, started in the directory it is a shell for.
    assert _arg(new, "-t") == "$1:"
    assert _arg(new, "-n") == shellwin.WINDOW_NAME
    assert _arg(new, "-c") == "/work/proj"
    # The window's command is the shell session itself, as one shell word.
    assert new[-1].endswith("run --shell --no-tmux --for=/work/proj claude")


def test_show_takes_a_joined_shell_back_out_into_its_own_window(monkeypatch):
    # It is in the agent's window as a pane; "show me the shell" there means
    # giving it the whole window back, not selecting the agent's.
    panes = [_pane(pane="%0", window="@0", index="0", panes="3")]
    panes.append(_pane(pane="%2", window="@0", index="0", panes="3", mark=CONTAINER))
    calls = _fake_tmux(monkeypatch, panes, session="$1", broke="4")
    outcome = shellwin.show(Path("/work/proj"), CONTAINER, "claude")

    assert (outcome.action, outcome.index) == (shellwin.SWITCHED, "4")
    assert _call(calls, "select-window") is None
    broke = _call(calls, "break-pane")
    assert _arg(broke, "-s") == "%2"
    assert _arg(broke, "-n") == shellwin.WINDOW_NAME
    # No -d: the point of the ask was to end up looking at it.
    assert "-d" not in broke


def test_show_marks_the_pane_with_its_container(monkeypatch):
    # On the pane, not the window: break-pane makes a new window every time,
    # so a mark on the window would be lost the first time it moved. And set
    # here rather than by the shell itself, which would leave it unmarked for
    # as long as the container takes to start — long enough for a second ask
    # to miss it and open a duplicate.
    calls = _fake_tmux(monkeypatch, [], session="$1")
    shellwin.show(Path("/work/proj"), CONTAINER, "claude")
    assert _call(calls, "set-option") == (
        "set-option",
        "-p",
        "-t",
        "%9",
        shellwin.MARK,
        CONTAINER,
    )


def test_show_outside_tmux_says_so(monkeypatch):
    _fake_tmux(monkeypatch, [])
    monkeypatch.delenv("TMUX")
    with pytest.raises(shellwin.Error):
        shellwin.show(Path("/work/proj"), CONTAINER, "claude")


def test_show_reports_a_pane_tmux_would_not_name(monkeypatch):
    # -P should always answer; if it somehow doesn't there is nothing to mark,
    # and an unmarked pane would be invisible to every later ask.
    _fake_tmux(monkeypatch, [], created="")
    with pytest.raises(shellwin.Error):
        shellwin.show(Path("/work/proj"), CONTAINER, "claude")


# ---------------------------------------------------------------------------
# show — a pane in the agent's window
# ---------------------------------------------------------------------------


def test_show_as_pane_splits_the_agents_pane_when_there_is_no_shell(monkeypatch):
    calls = _fake_tmux(monkeypatch, [_pane(pane="%0")], session="$1", window="@0")
    outcome = shellwin.show(Path("/work/proj"), CONTAINER, "claude", as_pane=True)

    assert outcome.action == shellwin.JOINED
    assert _call(calls, "new-window") is None
    split = _call(calls, "split-window")
    # The agent's pane specifically, so the shell lands under the agent and
    # above the monitor however the caller got here.
    assert _arg(split, "-t") == "@0.0"
    assert _arg(split, "-l") == str(shellwin.PANE_HEIGHT)
    assert "-v" in split


def test_show_as_pane_pulls_in_a_shell_that_is_in_its_own_window(monkeypatch):
    panes = [_pane(pane="%0", window="@0", index="0", panes="2"), _pane(mark=CONTAINER)]
    calls = _fake_tmux(monkeypatch, panes, session="$1", window="@0")
    outcome = shellwin.show(Path("/work/proj"), CONTAINER, "claude", as_pane=True)

    assert outcome.action == shellwin.JOINED
    join = _call(calls, "join-pane")
    assert _arg(join, "-s") == "%2"
    assert _arg(join, "-t") == "@0.0"
    assert _arg(join, "-l") == str(shellwin.PANE_HEIGHT)
    # Nothing is created: it is the same shell, moved.
    assert _call(calls, "split-window") is None


def test_show_as_pane_parks_a_shell_that_is_already_here(monkeypatch):
    # Asking again is what puts it back, and -d keeps our view where it is
    # rather than following the shell out.
    panes = [
        _pane(pane="%0", window="@0", index="0", panes="3"),
        _pane(pane="%2", window="@0", index="0", panes="3", mark=CONTAINER),
    ]
    calls = _fake_tmux(monkeypatch, panes, session="$1", window="@0", broke="4")
    outcome = shellwin.show(Path("/work/proj"), CONTAINER, "claude", as_pane=True)

    assert (outcome.action, outcome.index) == (shellwin.PARKED, "4")
    broke = _call(calls, "break-pane")
    assert "-d" in broke
    assert _arg(broke, "-s") == "%2"
    assert _call(calls, "join-pane") is None


def test_show_as_pane_from_the_shells_own_window_says_so(monkeypatch):
    # There is no agent window here to pull it into and nothing to push out:
    # tmux would treat the break as a no-op rather than an error, which would
    # leave us claiming to have moved something.
    _fake_tmux(monkeypatch, [_pane(mark=CONTAINER)], session="$1", window="@2")
    with pytest.raises(shellwin.Error):
        shellwin.show(Path("/work/proj"), CONTAINER, "claude", as_pane=True)


# ---------------------------------------------------------------------------
# Outcome.describe
# ---------------------------------------------------------------------------


def test_describe_names_the_key_that_reaches_a_window(monkeypatch):
    # The switch may not be visible from where the ask came from (a monitor
    # pane in a session group), so the line has to say where it went.
    assert shellwin.Outcome(shellwin.OPENED, "2").describe().endswith("(C-b 2)")
    assert "C-b" not in shellwin.Outcome(shellwin.JOINED, None).describe()
