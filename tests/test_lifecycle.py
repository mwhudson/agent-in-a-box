# Tests for the pure/host-side helpers in aiab.lifecycle.
#
# The proxy and stopper spawning need a live LXD and are exercised manually;
# the per-container lock probe is pure filesystem and covered here.

import fcntl
import os
# The background-session probe also needs no live LXD. Its parsing of the
# agent's output and conservative handling of failures are pinned below.
#
# It drives the idle-stopper's decision to keep a container up (see
# aiab.stopper).

from types import SimpleNamespace
from typing import Any

from aiab import lifecycle


def _lock_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "LOCK_DIR", tmp_path)
    return tmp_path


def test_session_in_use_false_without_a_lock_file(monkeypatch, tmp_path):
    # Nothing has ever run for this container, so there is no lock file. The
    # probe must not create one either: a container that is never built would
    # otherwise leave a stray file behind.
    _lock_dir(monkeypatch, tmp_path)
    assert lifecycle.session_in_use("claude-proj-abc123") is False
    assert list(tmp_path.iterdir()) == []


def test_session_in_use_false_when_the_lock_is_free(monkeypatch, tmp_path):
    # A previous session left the file behind but released the lock with its
    # process; the OS drops flocks on death, so an idle container reads as free.
    lock_dir = _lock_dir(monkeypatch, tmp_path)
    (lock_dir / "claude-proj-abc123").write_text("")
    assert lifecycle.session_in_use("claude-proj-abc123") is False


def test_session_in_use_true_while_a_session_holds_it_shared(monkeypatch, tmp_path):
    # What stop_when_idle() does for the life of a session. flock is keyed to
    # the open file description rather than the process, so a second descriptor
    # conflicts here exactly as another aiab process would.
    lock_dir = _lock_dir(monkeypatch, tmp_path)
    lock_path = lock_dir / "claude-proj-abc123"
    lock_path.write_text("")
    with lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_SH)
        assert lifecycle.session_in_use("claude-proj-abc123") is True


def test_session_in_use_leaves_the_lock_takeable(monkeypatch, tmp_path):
    # The probe acquires an exclusive lock when the container is idle; if it
    # failed to release it, the run that just probed could not then take its
    # own shared lock.
    lock_dir = _lock_dir(monkeypatch, tmp_path)
    lock_path = lock_dir / "claude-proj-abc123"
    lock_path.write_text("")
    assert lifecycle.session_in_use("claude-proj-abc123") is False
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # raises if still held
    finally:
        os.close(fd)


class FakeContainer:
    """Stands in for lxd.Container: canned status and exec result."""

    def __init__(self, status="RUNNING", exec_result=None, exec_error=None):
        self._status = status
        self._exec_result = exec_result
        self._exec_error = exec_error
        self.exec_calls = []

    def status(self):
        return self._status

    def exec(self, cmd, **kwargs):
        self.exec_calls.append((cmd, kwargs))
        if self._exec_error is not None:
            raise self._exec_error
        return self._exec_result


def _result(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


# `claude agents --json` output shapes: interactive entries carry a pid and
# kind "interactive"; background entries carry kind "background".
_INTERACTIVE = '[{"pid": 42, "kind": "interactive", "status": "busy"}]'
_BACKGROUND = '[{"id": "abc", "kind": "background", "name": "task"}]'


def test_agent_without_background_concept_is_never_live():
    # opencode has no background_ls; the probe must be skipped entirely.
    container: Any = FakeContainer(exec_result=_result(0, _BACKGROUND))
    assert lifecycle.has_live_background_session(container, "opencode") is False
    assert container.exec_calls == []


def test_stopped_container_is_never_live():
    container: Any = FakeContainer(status="STOPPED")
    assert lifecycle.has_live_background_session(container, "claude") is False
    assert container.exec_calls == []


def test_empty_session_list_is_not_live():
    container: Any = FakeContainer(exec_result=_result(0, "[]\n"))
    assert lifecycle.has_live_background_session(container, "claude") is False


def test_background_session_is_live():
    container: Any = FakeContainer(exec_result=_result(0, _BACKGROUND))
    assert lifecycle.has_live_background_session(container, "claude") is True


def test_only_an_interactive_session_is_not_live():
    # The foreground session that just exited (or a concurrent one) lingers in
    # the list as kind "interactive"; it must not, alone, count as background.
    container: Any = FakeContainer(exec_result=_result(0, _INTERACTIVE))
    assert lifecycle.has_live_background_session(container, "claude") is False


def test_background_alongside_interactive_is_live():
    both = '[{"pid": 42, "kind": "interactive"}, {"id": "a", "kind": "background"}]'
    container: Any = FakeContainer(exec_result=_result(0, both))
    assert lifecycle.has_live_background_session(container, "claude") is True


def test_probe_runs_the_agents_argv_as_the_container_user():
    from aiab import CONTAINER_USER

    cfg = lifecycle.agents.get("claude")
    assert cfg.background_ls is not None
    container: Any = FakeContainer(exec_result=_result(0, "[]"))
    lifecycle.has_live_background_session(container, "claude")
    assert len(container.exec_calls) == 1
    cmd, kwargs = container.exec_calls[0]
    assert cmd == [cfg.command, *cfg.background_ls]
    assert kwargs["user"] == CONTAINER_USER
    assert kwargs["check"] is False


def test_nonzero_exit_is_treated_as_not_live():
    container: Any = FakeContainer(exec_result=_result(1, "boom"))
    assert lifecycle.has_live_background_session(container, "claude") is False


def test_unparseable_output_is_treated_as_not_live():
    container: Any = FakeContainer(exec_result=_result(0, "not json"))
    assert lifecycle.has_live_background_session(container, "claude") is False


def test_exec_error_is_treated_as_not_live():
    container: Any = FakeContainer(exec_error=OSError("no such container"))
    assert lifecycle.has_live_background_session(container, "claude") is False
