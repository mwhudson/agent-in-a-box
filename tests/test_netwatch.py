# Tests for aiab.netwatch — the plumbing behind `aiab monitor` (the UI over
# it is covered in test_monitor_tui.py). This covers decision recording and
# the pending-queue helpers.

import pytest

import aiab.netproxy as netproxy
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


def test_pending_hosts_excludes_bookkeeping_entries(tmp_path):
    # The attached-monitor lock, and the pid file older versions used for the
    # same job, are not parked hosts.
    pdir = netwatch.pending_dir(tmp_path)
    pdir.mkdir(parents=True)
    (pdir / "example.com").write_text("0\n")
    (pdir / netproxy.WATCHER_LOCK).write_text("")
    (pdir / "watcher.pid").write_text("123\n")
    assert netwatch.pending_hosts(pdir) == {"example.com"}


def test_attached_is_visible_to_the_proxy(tmp_path):
    # What switches the proxy from fail-fast 403s to parking.
    pdir = netwatch.pending_dir(tmp_path)
    assert netproxy.watcher_attached(pdir) is False
    with netwatch.attached(pdir):
        assert netproxy.watcher_attached(pdir) is True
    assert netproxy.watcher_attached(pdir) is False


def test_attached_survives_the_latest_monitor_leaving_first(tmp_path):
    # The bug the lock replaces. The old marker was a single pid file holding
    # the most recent attacher, and that monitor removed it on exit — so when
    # it left while an earlier one was still running, parking silently switched
    # off for the survivor. The reverse order happened to work, which is why
    # this only shows up sometimes.
    pdir = netwatch.pending_dir(tmp_path)
    with netwatch.attached(pdir):  # the earlier monitor
        with netwatch.attached(pdir):  # attaches later, leaves first
            assert netproxy.watcher_attached(pdir) is True
        assert netproxy.watcher_attached(pdir) is True
    assert netproxy.watcher_attached(pdir) is False


def test_attached_ignores_the_order_monitors_leave_in(tmp_path):
    # The other order, which the pid file did handle. Entered by hand because
    # nesting can only express last-in-first-out.
    pdir = netwatch.pending_dir(tmp_path)
    earlier = netwatch.attached(pdir)
    earlier.__enter__()
    later = netwatch.attached(pdir)
    later.__enter__()
    assert netproxy.watcher_attached(pdir) is True
    earlier.__exit__(None, None, None)
    assert netproxy.watcher_attached(pdir) is True
    later.__exit__(None, None, None)
    assert netproxy.watcher_attached(pdir) is False


def test_attached_needs_no_cleanup_after_a_crash(tmp_path):
    # A monitor that dies leaves the lock file behind but not the lock, so the
    # proxy sees nothing attached without any liveness probing or pruning.
    pdir = netwatch.pending_dir(tmp_path)
    with netwatch.attached(pdir):
        pass
    assert (pdir / netproxy.WATCHER_LOCK).exists()
    assert netproxy.watcher_attached(pdir) is False
