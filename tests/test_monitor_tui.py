# Tests for aiab.monitor_tui — the textual front end for `aiab monitor`,
# driven headless through textual's test pilot. Skipped entirely when textual
# isn't installed (the CLI falls back to the plain console there, so there is
# nothing to test).

import asyncio

import pytest

import aiab.netproxy as netproxy
import aiab.netwatch as netwatch
import aiab.state as state

monitor_tui = pytest.importorskip("aiab.monitor_tui")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect policy, mount, pending-queue and proxy-log I/O to a tmp dir."""
    monkeypatch.setattr(state, "_NET_PATH", tmp_path / "network.json")
    monkeypatch.setattr(state, "_PATH", tmp_path / "mounts.json")
    monkeypatch.setattr(netwatch, "_PENDING_BASE", tmp_path / "pending")
    monkeypatch.setattr(netproxy, "PROXY_DIR", tmp_path / "proxy")


@pytest.fixture
def work_dir(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    netwatch.pending_dir(work).mkdir(parents=True)
    return work


def _new_app(work_dir):
    """A MonitorApp with live container ops stubbed out (no LXD in tests)."""
    app = monitor_tui.MonitorApp(work_dir)
    app._containers = lambda: []  # mount edits stay state-only, no lxc calls
    return app


def _pending_hosts_shown(app):
    return [row.host for row in app.query(monitor_tui.PendingRow)]


def _mounts_shown(app):
    return [(row.source, row.readonly) for row in app.query(monitor_tui.MountRow)]


# -- network view --


def test_key_decides_oldest_pending_host(work_dir):
    pdir = netwatch.pending_dir(work_dir)

    async def scenario():
        app = _new_app(work_dir)
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
        app = _new_app(work_dir)
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
        app = _new_app(work_dir)
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
        app = _new_app(work_dir)
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


# -- mounts view --


def test_toggle_shows_mounts_view(work_dir, tmp_path):
    extra = tmp_path / "lib"
    extra.mkdir()
    state.set_mount(work_dir, extra, readonly=True)

    async def scenario():
        app = _new_app(work_dir)
        async with app.run_test() as pilot:
            assert app.query_one("#mounts").display is False
            await pilot.press("m")
            await pilot.pause()
            assert app.query_one("#mounts").display is True
            assert app.query_one("#log").display is False
            assert _mounts_shown(app) == [(str(extra.resolve()), True)]

    asyncio.run(scenario())


def test_add_mount_records_it(work_dir, tmp_path):
    extra = tmp_path / "data"
    extra.mkdir()

    async def scenario():
        app = _new_app(work_dir)
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            inp = app.query_one("#add-path", monitor_tui.Input)
            inp.focus()
            inp.value = str(extra)
            await pilot.press("enter")
            await pilot.pause()

            assert state.get_mounts(work_dir) == [
                {"source": str(extra.resolve()), "readonly": True}
            ]
            assert _mounts_shown(app) == [(str(extra.resolve()), True)]
            assert inp.value == ""  # input cleared after submit

    asyncio.run(scenario())


def test_add_missing_directory_is_rejected(work_dir, tmp_path):
    async def scenario():
        app = _new_app(work_dir)
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            inp = app.query_one("#add-path", monitor_tui.Input)
            inp.focus()
            inp.value = str(tmp_path / "does-not-exist")
            await pilot.press("enter")
            await pilot.pause()
            assert state.get_mounts(work_dir) == []

    asyncio.run(scenario())


def test_toggle_mode_flips_readonly(work_dir, tmp_path):
    extra = tmp_path / "src"
    extra.mkdir()
    state.set_mount(work_dir, extra, readonly=True)

    async def scenario():
        app = _new_app(work_dir)
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            await pilot.click("MountRow .mode")
            await pilot.pause()
            assert state.get_mounts(work_dir) == [
                {"source": str(extra.resolve()), "readonly": False}
            ]
            assert _mounts_shown(app) == [(str(extra.resolve()), False)]

    asyncio.run(scenario())


def test_remove_button_drops_mount(work_dir, tmp_path):
    extra = tmp_path / "scratch"
    extra.mkdir()
    state.set_mount(work_dir, extra, readonly=False)

    async def scenario():
        app = _new_app(work_dir)
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            await pilot.click("MountRow .remove")
            await pilot.pause()
            assert state.get_mounts(work_dir) == []
            assert _mounts_shown(app) == []

    asyncio.run(scenario())
