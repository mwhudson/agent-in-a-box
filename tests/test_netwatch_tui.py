# Tests for aiab.netwatch_tui — the textual front end for `aiab net watch`,
# driven headless through textual's test pilot. Skipped entirely when textual
# isn't installed (the CLI falls back to the plain console there, so there is
# nothing to test).

import asyncio

import pytest

import aiab.netproxy as netproxy
import aiab.netwatch as netwatch
import aiab.state as state

netwatch_tui = pytest.importorskip("aiab.netwatch_tui")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect policy, pending-queue and proxy-log I/O to a tmp dir."""
    monkeypatch.setattr(state, "_NET_PATH", tmp_path / "network.json")
    monkeypatch.setattr(netwatch, "_PENDING_BASE", tmp_path / "pending")
    monkeypatch.setattr(netproxy, "PROXY_DIR", tmp_path / "proxy")


@pytest.fixture
def work_dir(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    netwatch.pending_dir(work).mkdir(parents=True)
    return work


def _pending_hosts_shown(app):
    return [row.host for row in app.query(netwatch_tui.PendingRow)]


def test_key_decides_oldest_pending_host(work_dir):
    pdir = netwatch.pending_dir(work_dir)

    async def scenario():
        app = netwatch_tui.WatchApp(work_dir)
        async with app.run_test() as pilot:
            (pdir / "example.com").write_text("0\n")
            app._poll()
            await pilot.pause()
            assert _pending_hosts_shown(app) == ["example.com"]

            await pilot.press("a")
            await pilot.pause()
            allows = state.get_network(work_dir)["allow"]
            assert [a["domain"] for a in allows] == ["example.com"]
            assert _pending_hosts_shown(app) == []

    asyncio.run(scenario())


def test_click_deny_button(work_dir):
    pdir = netwatch.pending_dir(work_dir)

    async def scenario():
        app = netwatch_tui.WatchApp(work_dir)
        async with app.run_test() as pilot:
            (pdir / "tracker.example").write_text("0\n")
            app._poll()
            await pilot.pause()

            await pilot.click("PendingRow Button.deny")
            await pilot.pause()
            assert state.get_network(work_dir)["deny"] == ["tracker.example"]
            assert _pending_hosts_shown(app) == []

    asyncio.run(scenario())


def test_decided_host_not_reprompted_while_file_remains(work_dir):
    # The proxy removes a pending file only on its next poll; until then the
    # just-decided host must not get a fresh row.
    pdir = netwatch.pending_dir(work_dir)

    async def scenario():
        app = netwatch_tui.WatchApp(work_dir)
        async with app.run_test() as pilot:
            (pdir / "example.com").write_text("0\n")
            app._poll()
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()

            app._poll()  # file still on disk
            await pilot.pause()
            assert _pending_hosts_shown(app) == []

    asyncio.run(scenario())


def test_vanished_file_removes_row_and_reappearance_reprompts(work_dir):
    # A request that times out takes its pending file (and row) with it; a
    # retry recreates the file and must prompt again.
    pdir = netwatch.pending_dir(work_dir)

    async def scenario():
        app = netwatch_tui.WatchApp(work_dir)
        async with app.run_test() as pilot:
            (pdir / "example.com").write_text("0\n")
            app._poll()
            await pilot.pause()
            assert _pending_hosts_shown(app) == ["example.com"]

            (pdir / "example.com").unlink()
            app._poll()
            await pilot.pause()
            assert _pending_hosts_shown(app) == []

            (pdir / "example.com").write_text("0\n")
            app._poll()
            await pilot.pause()
            assert _pending_hosts_shown(app) == ["example.com"]

    asyncio.run(scenario())
