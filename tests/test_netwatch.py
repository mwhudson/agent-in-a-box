# Tests for aiab.netwatch — the plumbing behind `aiab monitor` (the UI over
# it is covered in test_monitor_tui.py). This covers decision recording and
# the pending-queue helpers.

import os

import pytest

import aiab.netwatch as netwatch
import aiab.state as state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect policy and pending-queue I/O to a temporary directory."""
    monkeypatch.setattr(state, "_NET_PATH", tmp_path / "network.json")
    monkeypatch.setattr(netwatch, "_PENDING_BASE", tmp_path / "pending")


# ---------------------------------------------------------------------------
# apply_decision
# ---------------------------------------------------------------------------


def test_apply_decision_allow(tmp_path):
    message = netwatch.apply_decision(tmp_path, "example.com", netwatch.ALLOW)
    assert message == "allowed example.com"
    allows = state.get_network(tmp_path)["allow"]
    assert allows == [{"domain": "example.com", "expires": None}]


def test_apply_decision_temp_allow_expires(tmp_path):
    netwatch.apply_decision(tmp_path, "example.com", netwatch.TEMP)
    (allow,) = state.get_network(tmp_path)["allow"]
    assert allow["expires"] is not None  # _unexpired filters lapsed grants


def test_apply_decision_deny(tmp_path):
    message = netwatch.apply_decision(tmp_path, "example.com", netwatch.DENY)
    assert message == "denied example.com"
    assert state.get_network(tmp_path)["deny"] == ["example.com"]


def test_apply_decision_skip_records_nothing(tmp_path):
    netwatch.apply_decision(tmp_path, "example.com", netwatch.SKIP)
    policy = state.get_network(tmp_path)
    assert policy["allow"] == []
    assert policy["deny"] == []


# ---------------------------------------------------------------------------
# pending_hosts / attached
# ---------------------------------------------------------------------------


def test_pending_hosts_missing_dir(tmp_path):
    assert netwatch.pending_hosts(tmp_path / "nonexistent") == set()


def test_pending_hosts_excludes_watcher_pid(tmp_path):
    pdir = netwatch.pending_dir(tmp_path)
    pdir.mkdir(parents=True)
    (pdir / "example.com").write_text("0\n")
    (pdir / "watcher.pid").write_text("123\n")
    assert netwatch.pending_hosts(pdir) == {"example.com"}


def test_attached_marks_and_unmarks(tmp_path):
    pdir = netwatch.pending_dir(tmp_path)
    with netwatch.attached(pdir):
        assert (pdir / "watcher.pid").read_text() == f"{os.getpid()}\n"
    assert not (pdir / "watcher.pid").exists()


def test_attached_leaves_a_successors_marker(tmp_path):
    # A second watch session that has taken over the queue must keep its
    # marker when the first one exits.
    pdir = netwatch.pending_dir(tmp_path)
    with netwatch.attached(pdir):
        (pdir / "watcher.pid").write_text("99999999\n")
    assert (pdir / "watcher.pid").read_text() == "99999999\n"
