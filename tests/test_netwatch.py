# Tests for aiab.netwatch — the plumbing behind `aiab monitor` (the UI over
# it is covered in test_monitor_tui.py). This covers decision recording and
# the pending-queue helpers.

import json

import pytest

import aiab.netproxy as netproxy
import aiab.netwatch as netwatch
import aiab.state as state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect policy and pending-queue I/O to a temporary directory."""
    monkeypatch.setattr(state, "_NET_PATH", tmp_path / "network.json")
    monkeypatch.setattr(netwatch, "_PENDING_BASE", tmp_path / "pending")
    proxy_dir = tmp_path / "proxy"
    proxy_dir.mkdir()
    monkeypatch.setattr(netproxy, "PROXY_DIR", proxy_dir)


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


# ---------------------------------------------------------------------------
# read_usage: the per-host byte counters, read back from what the proxies
# publish (one file per session container; see netproxy._UsageMeter)
# ---------------------------------------------------------------------------


def _publish(work_dir, hosts, which=0):
    """Write one session container's counters, as its proxy would.

    `which` picks among the containers the directory could be running, so a
    test can put traffic in two of them at once.
    """
    name, _label = netwatch.session_containers(work_dir)[which]
    netproxy.usage_path(name).write_text(json.dumps({"hosts": hosts}))


def test_read_usage_with_nothing_published(tmp_path):
    # The ordinary case outside a restricted session: no proxy has run, so
    # there are no files and no traffic — not an error.
    assert netwatch.read_usage(tmp_path).hosts == {}


def test_read_usage_reports_a_hosts_counts(tmp_path):
    _publish(tmp_path, {"registry.npmjs.org": {"up": 10, "down": 2000}})
    usage = netwatch.read_usage(tmp_path)
    assert usage.hosts["registry.npmjs.org"] == netwatch.HostBytes(10, 2000)


def test_read_usage_sums_across_session_containers(tmp_path):
    # apt fetching in two of a directory's containers is the same traffic to
    # the same place; the directory's figure is the total.
    _publish(tmp_path, {"deb.example": {"up": 1, "down": 100}}, which=0)
    _publish(tmp_path, {"deb.example": {"up": 2, "down": 200}}, which=1)
    usage = netwatch.read_usage(tmp_path)
    assert usage.hosts["deb.example"] == netwatch.HostBytes(3, 300)


def test_read_usage_survives_a_damaged_file(tmp_path):
    # A truncated or hand-edited file must not take the monitor down: this is
    # a readout, and a session is not worth interrupting over it.
    name, _label = netwatch.session_containers(tmp_path)[0]
    netproxy.usage_path(name).write_text("{not json")
    assert netwatch.read_usage(tmp_path).hosts == {}


def test_read_usage_skips_unreadable_host_entries(tmp_path):
    # One malformed entry loses that host's figure, not the whole file's.
    _publish(
        tmp_path,
        {
            "good.example": {"up": 1, "down": 2},
            "bad.example": {"up": "lots"},
        },
    )
    usage = netwatch.read_usage(tmp_path)
    assert set(usage.hosts) == {"good.example"}


def test_totals_add_up_every_host(tmp_path):
    # What the header shows: one figure for the session, every host in it.
    _publish(
        tmp_path,
        {
            "api.anthropic.com": {"up": 1000, "down": 90000},
            "registry.npmjs.org": {"up": 10, "down": 500},
            "deb.example": {"up": 5, "down": 300},
        },
    )
    assert netwatch.read_usage(tmp_path).totals() == netwatch.HostBytes(1015, 90800)


def test_under_covers_a_rule_and_its_subdomains(tmp_path):
    # A rule for github.com is what allowed codeload.github.com, so the row
    # for that rule accounts for it.
    _publish(
        tmp_path,
        {
            "codeload.github.com": {"up": 1, "down": 5000},
            "github.com": {"up": 2, "down": 100},
            "nothub.com": {"up": 9, "down": 9},
        },
    )
    usage = netwatch.read_usage(tmp_path)
    assert usage.under("github.com") == netwatch.HostBytes(3, 5100)
    assert usage.under("codeload.github.com") == netwatch.HostBytes(1, 5000)
    assert usage.under("absent.example") == netwatch.HostBytes(0, 0)


# ---------------------------------------------------------------------------
# format_bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (9999, "10.0k"),
        (10_000, "10k"),
        (812_345, "812k"),
        (1_200_000, "1.2M"),
        (48_000_000, "48M"),
        (3_400_000_000, "3.4G"),
    ],
)
def test_format_bytes_fits_a_one_line_header(count, expected):
    assert netwatch.format_bytes(count) == expected
    assert len(netwatch.format_bytes(count)) <= 5
