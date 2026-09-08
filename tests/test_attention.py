# Tests for aiab.attention — the hooks that tell the host when an agent is
# waiting on the user, and the host-side reading of what they record. The
# notification raised from it is covered in test_monitor_tui.py.

import json
import shlex
from typing import Any

import pytest

import aiab.agents as agents
import aiab.attention as attention
import aiab.state as state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect the per-directory state dir (the mount both sides share)."""
    monkeypatch.setattr(state, "_DIRSTATE_DIR", tmp_path / "dirstate")


@pytest.fixture
def work_dir(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    return work


class _FakeContainer:
    """Records what would have been run in the container."""

    def __init__(self):
        self.execs = []

    def exec(self, cmd, **kwargs):
        self.execs.append((cmd, kwargs.get("input")))


# ---------------------------------------------------------------------------
# the drop-in
# ---------------------------------------------------------------------------


def _commands(key, event):
    hooks = json.loads(attention.drop_in(key))["hooks"][event]
    return [h["command"] for entry in hooks for h in entry["hooks"]]


def test_turn_end_and_prompts_record_a_wait():
    for event in ("Stop", "Notification"):
        (command,) = _commands("claude", event)
        assert command.endswith("> /aiab/attention/claude")


def test_answering_and_ending_clear_the_wait():
    for event in ("UserPromptSubmit", "SessionEnd"):
        assert _commands("claude", event) == ["rm -f /aiab/attention/claude"]


def test_only_waiting_notifications_are_matched():
    (entry,) = json.loads(attention.drop_in("claude"))["hooks"]["Notification"]
    types = entry["matcher"].split("|")
    assert "permission_prompt" in types
    # Not every notification means the agent stopped for you.
    assert "auth_success" not in types
    assert "agent_completed" not in types


def test_a_profile_session_gets_its_own_file():
    # A profile's key carries an '@' (profiles.home_key), and the commands are
    # shell, so the path has to come out both distinct and safe to run.
    (command,) = _commands("claude@openrouter", "UserPromptSubmit")
    assert command == "rm -f /aiab/attention/claude@openrouter"
    assert shlex.split(command)[-1] == "/aiab/attention/claude@openrouter"


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_writes_the_drop_in(work_dir):
    container: Any = _FakeContainer()
    attention.install(container, work_dir, "claude", attention.HOOKS)
    cmd, stdin = container.execs[0]
    assert cmd[0] == "sh"
    assert attention.DROP_IN_PATH in cmd[2]
    assert json.loads(stdin.decode())["hooks"]["Stop"]


def test_install_clears_a_wait_left_by_a_killed_session(work_dir):
    attention.attention_dir(work_dir).mkdir(parents=True)
    (attention.attention_dir(work_dir) / "claude").write_text("Waiting\n")

    container: Any = _FakeContainer()
    attention.install(container, work_dir, "claude", attention.HOOKS)
    assert attention.waiting(work_dir) == {}


def test_install_leaves_another_agents_wait_alone(work_dir):
    attention.attention_dir(work_dir).mkdir(parents=True)
    (attention.attention_dir(work_dir) / "opencode").write_text("Waiting\n")

    container: Any = _FakeContainer()
    attention.install(container, work_dir, "claude", attention.HOOKS)
    assert set(attention.waiting(work_dir)) == {"opencode"}


def test_install_writes_nothing_for_a_plugin_agent(work_dir):
    # opencode's plugin arrives as an overlay, mounted fresh every run; there
    # is nothing to write into the container.
    container: Any = _FakeContainer()
    attention.install(container, work_dir, "opencode", attention.PLUGIN)
    assert container.execs == []


def test_install_clears_a_plugin_agents_stale_wait(work_dir):
    attention.attention_dir(work_dir).mkdir(parents=True)
    (attention.attention_dir(work_dir) / "opencode").write_text("Waiting\n")

    container: Any = _FakeContainer()
    attention.install(container, work_dir, "opencode", attention.PLUGIN)
    assert attention.waiting(work_dir) == {}


# ---------------------------------------------------------------------------
# the plugin
# ---------------------------------------------------------------------------


def test_plugin_is_told_which_file_to_write():
    assert attention.env("opencode", attention.PLUGIN) == {
        "AIAB_ATTENTION": "/aiab/attention/opencode"
    }
    # A profile session writes its own file, same as a hooked agent's.
    assert attention.env("opencode@zen", attention.PLUGIN) == {
        "AIAB_ATTENTION": "/aiab/attention/opencode@zen"
    }


def test_hook_agents_need_no_environment():
    assert attention.env("claude", attention.HOOKS) == {}


def test_plugin_reports_the_same_reasons_the_monitor_shows():
    # The plugin is the container half of this module and writes the reason
    # text itself; the two have to agree on the wording.
    plugin = agents.REPO_ROOT / "agent-config/opencode/plugins/attention.js"
    source = plugin.read_text()
    assert attention._WAITING_FOR_PROMPT in source
    assert attention._WAITING_FOR_ANSWER in source
    assert attention.ENV_VAR in source


# ---------------------------------------------------------------------------
# reading the waits back
# ---------------------------------------------------------------------------


def test_no_waits_before_anything_has_run(work_dir):
    assert attention.waiting(work_dir) == {}


def test_wait_reports_its_reason_and_when_it_started(work_dir):
    adir = attention.attention_dir(work_dir)
    adir.mkdir(parents=True)
    (adir / "claude").write_text("Waiting for your next prompt\n")

    since, reason = attention.waiting(work_dir)["claude"]
    assert reason == "Waiting for your next prompt"
    assert since == pytest.approx((adir / "claude").stat().st_mtime)


def test_half_written_file_still_counts_as_waiting(work_dir):
    # The container writes the file while the host may be reading it; an empty
    # read must not lose the wait, only its wording.
    adir = attention.attention_dir(work_dir)
    adir.mkdir(parents=True)
    (adir / "claude").write_text("")

    _since, reason = attention.waiting(work_dir)["claude"]
    assert reason
