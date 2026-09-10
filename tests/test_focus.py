# Tests for aiab.focus — the tmux questions behind "are you looking at the
# agent, and have you touched its keyboard". tmux itself is stubbed out here:
# what each format string means was verified against a real tmux (3.6), and
# what this module has to get right is the reading of the answers.

import pytest

import aiab.focus as focus


class _FakeTime:
    """A clock the test moves, standing in for the time module focus uses."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(focus, "time", fake)
    return fake


def _client(flags="attached,focused,UTF-8", activity=100, window="@1"):
    """One `tmux list-clients` line in the format aiab.focus asks for."""
    return f"{flags}\t{activity}\t{window}\n"


def _fake_tmux(monkeypatch, clients, *, pane="@1", enabled=True):
    """Answer tmux's part of the conversation; return the calls it was asked.

    `clients` is a list the test can rewrite between looks, so one Focus can
    be walked through a sequence of terminal states.
    """
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "display-message":
            return f"{pane}\n" if pane else ""
        if args[0] == "set-option":
            return ""
        if args[0] == "show-options":
            return f"focus-events {'on' if enabled else 'off'}\n"
        if args[0] == "list-clients":
            return "".join(clients)
        return None

    monkeypatch.setattr(focus, "_tmux", run)
    return calls


def test_outside_tmux_nothing_is_known(monkeypatch, clock):
    calls = _fake_tmux(monkeypatch, [_client()])
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert focus.Focus().look().focused is None
    # And tmux was never even asked, let alone told to change an option.
    assert calls == []


def test_enabling_focus_reporting_reads_back_what_it_set(monkeypatch, clock):
    calls = _fake_tmux(monkeypatch, [])
    assert focus.enable() is True
    assert calls[0] == ("set-option", "-s", "focus-events", "on")


def test_enabling_reports_a_tmux_that_would_not(monkeypatch, clock):
    # An old tmux without the option: better to know than to quietly stop
    # announcing waits.
    _fake_tmux(monkeypatch, [], enabled=False)
    assert focus.enable() is False


def test_looking_never_changes_an_option(monkeypatch, clock):
    # By the time an agent is waiting the terminal attached long ago, and
    # turning focus reporting on now would not reach it (see aiab.focus).
    calls = _fake_tmux(monkeypatch, [_client()])
    watcher = focus.Focus(pane="%3")
    for _ in range(3):
        watcher.look()
    assert [c for c in calls if c[0] == "set-option"] == []


def test_focus_reporting_left_off_is_not_believed(monkeypatch, clock):
    _fake_tmux(monkeypatch, [_client()], enabled=False)
    assert focus.Focus(pane="%3").look().focused is None


def test_a_client_showing_our_window_is_you(monkeypatch, clock):
    clients = [_client(flags="attached,UTF-8")]
    _fake_tmux(monkeypatch, clients)
    watcher = focus.Focus(pane="%3")
    # Unfocused once, so the flag is being reported rather than assumed.
    assert watcher.look().focused is False
    clients[:] = [_client()]
    assert watcher.look() == focus.Look(focused=True, typed=False)


def test_a_client_looking_elsewhere_is_not(monkeypatch, clock):
    _fake_tmux(monkeypatch, [_client(window="@7")])
    assert focus.Focus(pane="%3").look().focused is False


def test_a_window_nobody_is_attached_to_is_not_focused(monkeypatch, clock):
    _fake_tmux(monkeypatch, [])
    assert focus.Focus(pane="%3").look().focused is False


def test_a_terminal_that_never_reports_focus_is_not_believed(monkeypatch, clock):
    # A client that attached before focus reporting went on is flagged
    # focused for as long as it lives, whatever its terminal could report.
    clients = [_client()]
    _fake_tmux(monkeypatch, clients)
    watcher = focus.Focus(pane="%3")
    for _ in range(3):
        assert watcher.look().focused is None

    # Seeing it go unfocused settles the question for good.
    clients[:] = [_client(flags="attached,UTF-8")]
    assert watcher.look().focused is False
    clients[:] = [_client()]
    assert watcher.look().focused is True


def test_a_keystroke_shows_up_as_typing(monkeypatch, clock):
    clients = [_client(flags="attached,UTF-8", activity=100)]
    _fake_tmux(monkeypatch, clients)
    watcher = focus.Focus(pane="%3")
    assert watcher.look().focused is False

    clock.sleep(0.3)
    clients[:] = [_client(activity=100)]
    assert watcher.look() == focus.Look(focused=True, typed=False)

    clock.sleep(0.3)
    clients[:] = [_client(activity=101)]
    assert watcher.look() == focus.Look(focused=True, typed=True)

    # And the same reading again is not a second keystroke.
    clock.sleep(0.3)
    assert watcher.look() == focus.Look(focused=True, typed=False)


def test_taking_focus_is_not_a_keystroke(monkeypatch, clock):
    # The terminal reports focus with an escape sequence, which is input as
    # far as tmux is concerned: the activity time moves without you typing.
    clients = [_client(flags="attached,UTF-8", activity=100)]
    _fake_tmux(monkeypatch, clients)
    watcher = focus.Focus(pane="%3")
    assert watcher.look().focused is False

    clock.sleep(0.3)
    clients[:] = [_client(activity=101)]
    assert watcher.look() == focus.Look(focused=True, typed=False)


def test_a_stale_reading_is_not_measured_against(monkeypatch, clock):
    clients = [_client(flags="attached,UTF-8", activity=100)]
    _fake_tmux(monkeypatch, clients)
    watcher = focus.Focus(pane="%3")
    watcher.look()
    clients[:] = [_client(activity=100)]
    watcher.look()

    # Nothing looked for a while — the monitor only asks while an agent is
    # waiting — so a key pressed since could as easily have been about the
    # last wait as this one.
    clock.sleep(focus._STALE + 1)
    clients[:] = [_client(activity=400)]
    assert watcher.look() == focus.Look(focused=True, typed=False)


def test_the_busiest_client_is_the_one_that_counts(monkeypatch, clock):
    # Two terminals on the same window: focus and keystrokes from either are
    # yours.
    clients = [_client(flags="attached,UTF-8", activity=100), _client(activity=100)]
    _fake_tmux(monkeypatch, clients)
    watcher = focus.Focus(pane="%3")
    assert watcher.look().focused is True

    clock.sleep(0.3)
    clients[:] = [_client(flags="attached,UTF-8", activity=100), _client(activity=105)]
    assert watcher.look().typed is True
