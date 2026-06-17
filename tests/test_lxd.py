# Tests for the pure helper functions in aiab.lxd.

import json
import subprocess

import pytest

from aiab import lxd
from aiab.lxd import Container, Lxd, container_name_for_dir, dir_slug, is_source_device


# ---------------------------------------------------------------------------
# container_name_for_dir
# ---------------------------------------------------------------------------


def test_name_contains_basename():
    name = container_name_for_dir("/home/user/my-project", "claude")
    assert "my-project" in name


def test_name_prefixed_with_agent():
    name = container_name_for_dir("/home/user/my-project", "claude")
    assert name.startswith("claude-")


def test_name_is_stable():
    a = container_name_for_dir("/home/user/my-project", "claude")
    b = container_name_for_dir("/home/user/my-project", "claude")
    assert a == b


def test_same_basename_different_dirs_differ():
    a = container_name_for_dir("/home/alice/project", "claude")
    b = container_name_for_dir("/home/bob/project", "claude")
    assert a != b


def test_name_only_lowercase_alnum_and_hyphens():
    name = container_name_for_dir("/home/user/My Project!", "claude")
    # LXD container names must be lowercase alphanumeric + hyphens.
    import re

    assert re.fullmatch(r"[a-z0-9-]+", name)


def test_name_no_leading_or_trailing_hyphen_in_basename_part():
    # Paths with leading/trailing special chars in basename shouldn't produce
    # double-hyphens at the basename boundary.
    name = container_name_for_dir("/home/user/---weird---", "claude")
    assert "--" not in name


def test_name_length_within_lxd_limit():
    long_path = "/home/user/" + "a" * 200
    name = container_name_for_dir(long_path, "claude")
    # LXD container names are capped at 63 chars.
    assert len(name) <= 63


# ---------------------------------------------------------------------------
# dir_slug
# ---------------------------------------------------------------------------


def test_container_name_is_prefixed_slug():
    # The per-directory state dirs (aiab.state) are named with the bare slug,
    # so this keeps them correlatable with the container names.
    path = "/home/user/my-project"
    assert container_name_for_dir(path, "claude") == "claude-" + dir_slug(path)


def test_dir_slug_sanitised():
    import re

    assert re.fullmatch(r"[a-z0-9-]+", dir_slug("/home/user/My Project!"))


# ---------------------------------------------------------------------------
# is_source_device
# ---------------------------------------------------------------------------


def test_source_device_recognised():
    # container_name_for_dir embeds the last 6 chars of the path hash as the
    # container name suffix; is_source_device does a prefix match on the
    # dir-<hash> device name.
    import hashlib

    path = "/home/user/my-project"
    container_name = container_name_for_dir(path, "claude")
    path_hash = container_name.rsplit("-", 1)[-1]  # 6-char suffix
    device_name = f"dir-{path_hash}abcd"  # longer hash, as _device_name produces
    assert is_source_device(device_name, container_name)


def test_non_source_device_not_recognised():
    container_name = container_name_for_dir("/home/user/project", "claude")
    # A device with an unrelated hash should not match.
    assert not is_source_device("dir-zzzzzzzz", container_name)


def test_extra_mount_not_recognised():
    # A second directory mounted into the same container has a different hash.
    import hashlib

    container_name = container_name_for_dir("/home/user/project", "claude")
    other_hash = hashlib.md5(b"/home/user/other").hexdigest()[:8]
    assert not is_source_device(f"dir-{other_hash}", container_name)


# ---------------------------------------------------------------------------
# snapshot caching (status/config/devices share one `lxc query`)
# ---------------------------------------------------------------------------


def _fake_lxc(monkeypatch, snapshot):
    """Patch lxd's subprocess.run; return a list recording every lxc subcommand.

    `query` calls return the given snapshot as JSON; every other call returns a
    successful empty result. The recorded list holds the lxc verb(s) of each
    call (e.g. "query", "config set", "config device remove") so tests can
    assert how many round-trips happened.
    """
    calls: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        # `lxc query` carries the project in the URL, not via --project, so it
        # has no --project prefix; everything else is ["lxc","--project",P,...].
        if "query" in cmd:
            calls.append("query")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(snapshot), "")
        rest = cmd[3:]  # skip ["lxc", "--project", P]
        calls.append(" ".join(rest[:2]) if len(rest) > 1 else rest[0])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(lxd.subprocess, "run", fake_run)
    return calls


def _container(monkeypatch, snapshot):
    calls = _fake_lxc(monkeypatch, snapshot)
    return Container(Lxd("aiab"), "c1"), calls


SNAP = {
    "status": "Running",
    "config": {"limits.cpu": "2", "user.aiab_base": "noble"},
    "devices": {"dir-abc": {"type": "disk", "source": "/work/x", "path": "/work/x"}},
}


def test_reads_share_one_query(monkeypatch):
    c, calls = _container(monkeypatch, SNAP)
    assert c.exists() is True
    assert c.status() == "RUNNING"  # API "Running" upper-cased
    assert c.get_config("user.aiab_base") == "noble"
    assert "dir-abc" in c.devices()
    assert calls.count("query") == 1  # all four reads served from one snapshot


def test_query_passes_project_in_url_not_flag(monkeypatch):
    # `lxc query` ignores the global --project flag; the project must ride in
    # the URL as ?project=. Capture the exact argv used for the snapshot.
    seen = {}

    def fake_run(cmd, *args, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps(SNAP), "")

    monkeypatch.setattr(lxd.subprocess, "run", fake_run)
    Container(Lxd("aiab"), "c1").snapshot()
    assert seen["cmd"] == ["lxc", "query", "/1.0/instances/c1?project=aiab"]
    assert "--project" not in seen["cmd"]


def test_status_empty_when_absent(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "not found")

    monkeypatch.setattr(lxd.subprocess, "run", fake_run)
    c = Container(Lxd("aiab"), "missing")
    assert c.exists() is False
    assert c.status() == ""
    assert c.get_config("anything") == ""
    assert c.devices() == {}


def test_mutation_invalidates_cache(monkeypatch):
    c, calls = _container(monkeypatch, SNAP)
    c.devices()  # query #1
    c.remove_device("dir-abc")  # mutates -> invalidates
    c.devices()  # query #2 (fresh)
    assert calls.count("query") == 2
    assert "config device" in calls  # the remove round-trip happened


def test_set_config_skips_unchanged(monkeypatch):
    c, calls = _container(monkeypatch, SNAP)
    c.set_config("limits.cpu", "2")  # already 2 -> no write
    assert "config set" not in calls


def test_set_config_writes_when_changed(monkeypatch):
    c, calls = _container(monkeypatch, SNAP)
    c.set_config("limits.cpu", "4")  # differs -> write, cache updated in place
    assert "config set" in calls
    assert c.get_config("limits.cpu") == "4"  # served from updated cache
    assert calls.count("query") == 1  # no re-query after the write


# ---------------------------------------------------------------------------
# lazy project creation in run()
# ---------------------------------------------------------------------------


def test_run_creates_project_on_demand_then_retries(monkeypatch):
    lxd._ensured_projects.clear()
    project = {"exists": False}
    created: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["lxc", "project", "show"]:
            return subprocess.CompletedProcess(cmd, 0 if project["exists"] else 1)
        if cmd[:3] == ["lxc", "project", "create"]:
            created.append(cmd[3])
            project["exists"] = True
            return subprocess.CompletedProcess(cmd, 0)
        # A project-scoped command fails while the project is absent.
        if cmd[:2] == ["lxc", "--project"] and not project["exists"]:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(lxd.subprocess, "run", fake_run)
    result = lxd.run(["lxc", "--project", "aiab", "init", "img", "c1"])
    assert result.returncode == 0
    assert created == ["aiab"]  # created exactly once, on demand


def test_run_reraises_when_project_already_exists(monkeypatch):
    # A genuine failure (project present) must propagate, not be retried.
    lxd._ensured_projects.clear()

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["lxc", "project", "show"]:
            return subprocess.CompletedProcess(cmd, 0)  # exists
        if cmd[:3] == ["lxc", "project", "create"]:
            raise AssertionError("must not create an existing project")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(lxd.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        lxd.run(["lxc", "--project", "aiab", "init", "img", "c1"])


def test_run_does_not_probe_for_non_project_commands(monkeypatch):
    lxd._ensured_projects.clear()
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(lxd.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        lxd.run(["lxc", "project", "list"])  # no --project; nothing to create
    assert seen == [["lxc", "project", "list"]]  # no project show/create probe


def test_apply_limits_unchanged_is_free(monkeypatch):
    snap = {"status": "Running", "config": {"limits.cpu": "2", "limits.memory": "4GiB"}}
    c, calls = _container(monkeypatch, snap)
    c.apply_limits(cpu=2, memory="4GiB")
    assert "config set" not in calls
    assert calls.count("query") == 1
