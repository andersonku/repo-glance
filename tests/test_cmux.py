"""cmux agent state (running / waiting), the event filter, and jumping to it."""

from __future__ import annotations

import io
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from rg import cmux

WAITING = cmux.CMUX_WAITING_BODY


class FakeCmux:
    """Answers the cmux CLI calls _cmux_agent_states makes."""

    def __init__(self, workspaces, notes, running=()):
        self.workspaces, self.notes, self.running = workspaces, notes, set(running)

    def __call__(self, *args, timeout=5.0):
        if args == ("rpc", "workspace.list"):
            out = {"workspaces": self.workspaces}
        elif args == ("--json", "list-notifications"):
            out = self.notes
        elif args == ("--json", "tree", "--all"):
            out = {"windows": [{"workspaces": [
                {"id": w["id"], "panes": [{"surfaces": [
                    {"type": "terminal", "title": ("◑ Working" if w["id"] in self.running else "✳ Idle")},
                    {"type": "markdown", "title": "◐ not a terminal"},
                ]}]}
                for w in self.workspaces
            ]}]}
        else:
            raise AssertionError(f"unexpected cmux call {args}")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(out))


def ws(ws_id, cwd, submitted=None):
    return {"id": ws_id, "current_directory": cwd, "latest_submitted_at": submitted}


def note(ws_id, at, body=WAITING, note_id=None, read=True):
    return {"id": note_id or f"n-{ws_id}-{at}", "workspace_id": ws_id,
            "created_at": at, "body": body, "is_read": read}


class AgentStateTests(unittest.TestCase):
    repos = ["/r/playmaker", "/r/playmaker2"]

    def states(self, fake):
        with patch.object(cmux, "_cmux", fake):
            return {repo: (s.state, s.notification_id) for repo, s in
                    cmux._cmux_agent_states(self.repos).items()}

    def test_waiting_until_a_newer_prompt_is_submitted(self):
        fake = FakeCmux(
            [ws("a", "/r/playmaker", "2026-10-01T01:00:00.000Z")],
            [note("a", "2026-10-01T01:05:00Z", note_id="n1")],
        )
        self.assertEqual(self.states(fake), {"/r/playmaker": ("waiting", "n1")})
        fake.workspaces[0]["latest_submitted_at"] = "2026-10-01T01:06:00.000Z"
        self.assertEqual(self.states(fake), {})

    def test_read_state_is_ignored_and_missing_submission_counts_as_unanswered(self):
        fake = FakeCmux([ws("a", "/r/playmaker")], [note("a", "2026-10-01T01:00:00Z", read=True)])
        self.assertEqual(self.states(fake)["/r/playmaker"][0], "waiting")

    def test_running_wins_over_waiting_in_another_workspace(self):
        fake = FakeCmux(
            [ws("a", "/r/playmaker"), ws("b", "/r/playmaker")],
            [note("a", "2026-10-01T01:00:00Z")],
            running={"b"},
        )
        self.assertEqual(self.states(fake), {"/r/playmaker": ("running", None)})

    def test_newest_waiting_notification_is_the_jump_target(self):
        fake = FakeCmux([ws("a", "/r/playmaker")], [
            note("a", "2026-10-01T01:00:00Z", note_id="old"),
            note("a", "2026-10-01T02:00:00Z", note_id="new"),
            note("a", "2026-10-01T03:00:00Z", body="Claude Code login successful", note_id="other"),
        ])
        self.assertEqual(self.states(fake)["/r/playmaker"], ("waiting", "new"))

    def test_subdirectories_count_but_sibling_prefixes_do_not(self):
        fake = FakeCmux(
            [ws("a", "/r/playmaker2/packages/api"), ws("b", "/r/playmaker20")],
            [note("a", "2026-10-01T01:00:00Z"), note("b", "2026-10-01T01:00:00Z")],
        )
        self.assertEqual(set(self.states(fake)), {"/r/playmaker2"})

    def test_stale_running_flag_is_ignored_when_no_tab_spins(self):
        # cmux's any_agent_running can stay on after an interrupted turn; the
        # tab titles are what decide, so this is "waiting", not "running".
        fake = FakeCmux([ws("a", "/r/playmaker")], [note("a", "2026-10-01T01:00:00Z")])
        self.assertEqual(self.states(fake)["/r/playmaker"][0], "waiting")

    def test_spinner_detection_reads_terminal_titles_only(self):
        tree = {"windows": [{"workspaces": [
            {"id": "w1", "panes": [{"surfaces": [{"type": "terminal", "title": "◒ Doing things"}]}]},
            {"id": "w2", "panes": [{"surfaces": [{"type": "terminal", "title": "✳ Done"}]}]},
            {"id": "w3", "panes": [{"surfaces": [{"type": "browser", "title": "◐ page"}]}]},
            {"id": "w4", "panes": [{"surfaces": [{"type": "terminal", "title": ""}]}]},
        ]}]}
        self.assertEqual(cmux._working_workspaces(tree), {"w1"})
        self.assertEqual(cmux._working_workspaces({}), set())

    def test_cmux_unavailable_means_no_states(self):
        def broken(*args, timeout=5.0):
            raise OSError("no cmux")
        with patch.object(cmux, "_cmux", broken):
            self.assertEqual(cmux._cmux_agent_states(self.repos), {})


class EventTests(unittest.TestCase):
    def frame(self, **fields):
        return json.dumps({"type": "event", "category": "agent", "name": "agent.hook.Stop", **fields})

    def test_relevant_events(self):
        self.assertTrue(cmux.relevant_event(self.frame()))
        self.assertTrue(cmux.relevant_event(self.frame(category="notification", name="notification.created")))
        self.assertTrue(cmux.relevant_event(self.frame(category="workspace", name="workspace.prompt.submitted")))

    def test_noise_is_ignored(self):
        self.assertFalse(cmux.relevant_event(self.frame(name="agent.hook.PreToolUse")))
        self.assertFalse(cmux.relevant_event(self.frame(category="feed", name="feed.item.received")))
        self.assertFalse(cmux.relevant_event(json.dumps({"type": "heartbeat"})))
        self.assertFalse(cmux.relevant_event("not json"))
        self.assertFalse(cmux.relevant_event("[]"))

    def test_watch_events_calls_back_once_per_relevant_event(self):
        lines = "\n".join([
            self.frame(), self.frame(name="agent.hook.PreToolUse"), "garbage",
            self.frame(category="notification", name="notification.removed"),
        ]) + "\n"
        proc = MagicMock(stdout=io.StringIO(lines))
        calls = []
        with patch.object(cmux.subprocess, "Popen", return_value=proc) as popen, \
                patch.object(cmux.atexit, "register"):
            cmux.watch_events(lambda: calls.append(1))
        self.assertEqual(len(calls), 2)
        args = popen.call_args.args[0]
        self.assertIn("--reconnect", args)
        self.assertNotIn("--no-heartbeat", args)  # heartbeats reap an orphaned child
        proc.kill.assert_called()


class OpenAgentTests(unittest.TestCase):
    def run_open(self, agent, returncode=0):
        calls = []

        def fake(*args, timeout=5.0):
            calls.append(args)
            return subprocess.CompletedProcess(args, returncode, stdout="")

        with patch.object(cmux, "_cmux", fake), patch.object(cmux, "_activate"), \
                patch.object(cmux, "open_in_cmux") as fallback:
            cmux.open_agent("/r/p", agent)
        return calls, fallback

    def test_waiting_opens_the_notification(self):
        calls, fallback = self.run_open(cmux.AgentState("waiting", "ws", "n1"))
        self.assertEqual(calls, [("open-notification", "--id", "n1")])
        fallback.assert_not_called()

    def test_running_selects_its_workspace(self):
        calls, fallback = self.run_open(cmux.AgentState("running", "ws"))
        self.assertEqual(calls, [("select-workspace", "--workspace", "ws")])
        fallback.assert_not_called()

    def test_falls_back_when_cmux_no_longer_knows_it(self):
        _calls, fallback = self.run_open(cmux.AgentState("waiting", "ws", "gone"), returncode=1)
        fallback.assert_called_once_with("/r/p")

    def test_no_agent_opens_the_repo(self):
        calls, fallback = self.run_open(None)
        self.assertEqual(calls, [])
        fallback.assert_called_once_with("/r/p")


if __name__ == "__main__":
    unittest.main()
