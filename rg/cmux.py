"""cmux integration: open/focus workspaces, per-repo agent state (running /
waiting for input), and the event stream that tells us when to recompute it."""

from __future__ import annotations

import atexit
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

CMUX_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"
CMUX_BUNDLE_ID = "com.cmuxterm.app"

CMUX_WAITING_BODY = "Claude is waiting for your input"

# Claude Code's terminal-title spinner while a turn is in progress (an idle
# session's title starts with "✳"). The tab title is the running signal:
# cmux's own any_agent_running flag stays stuck on when a turn ends without
# a Stop hook (interrupted with Esc, session killed mid-turn).
CLAUDE_SPINNER = frozenset("◐◓◑◒")

# Event categories that can change running/waiting: agent hooks (Stop,
# UserPromptSubmit, Notification, …), notifications, and prompt submissions.
CMUX_EVENT_CATEGORIES = ("agent", "notification", "workspace")
# Hooks that fire on every tool call and never change running/waiting.
_IGNORED_EVENTS = frozenset({"agent.hook.PreToolUse", "agent.hook.PostToolUse"})


def available() -> bool:
    return Path(CMUX_BIN).exists()


def _cmux(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [CMUX_BIN, *args], capture_output=True, text=True, timeout=timeout
    )


def _activate() -> None:
    subprocess.run(["open", "-b", CMUX_BUNDLE_ID], capture_output=True)


def _find_workspace(repo: str) -> tuple[str, str | None] | None:
    """Return (workspace_id, window_id) for the cmux workspace whose cwd is `repo`."""
    try:
        result = _cmux("rpc", "workspace.list")
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    window_id = data.get("window_id")
    for ws in data.get("workspaces", []):
        if ws.get("current_directory") == repo and ws.get("id"):
            return ws["id"], window_id
    return None


def open_in_cmux(repo: str) -> None:
    """Focus the cmux workspace for `repo`; open it in a new one if none exists."""
    found = _find_workspace(repo)
    try:
        if found:
            ws_id, window_id = found
            args = ["select-workspace", "--workspace", ws_id]
            if window_id:
                args += ["--window", window_id]
            _cmux(*args)
        else:
            # new-workspace --focus selects the new workspace; the bare-path
            # form doesn't. It needs cmux running, so fall back to the bare
            # form (which launches cmux) if it fails.
            result = _cmux("new-workspace", "--cwd", repo, "--focus", "true")
            if result.returncode != 0:
                _cmux(repo)
    except (subprocess.SubprocessError, OSError):
        return
    _activate()


@dataclass(frozen=True)
class AgentState:
    """What an agent is doing in one of a repo's cmux workspaces."""
    state: str  # "running" or "waiting"
    workspace_id: str
    # For "waiting": the "Claude is waiting for your input" notification, so a
    # click can jump to the exact tab (open-notification focuses its surface).
    notification_id: str | None = None


def open_agent(repo: str, agent: AgentState | None) -> None:
    """Jump to where the repo's agent is: the waiting tab for "waiting", the
    running workspace for "running", else plain open_in_cmux. Falls back to
    open_in_cmux if cmux no longer knows the notification/workspace."""
    try:
        if agent and agent.notification_id:
            if _cmux("open-notification", "--id", agent.notification_id).returncode == 0:
                return _activate()
        elif agent:
            if _cmux("select-workspace", "--workspace", agent.workspace_id).returncode == 0:
                return _activate()
    except (subprocess.SubprocessError, OSError):
        pass
    open_in_cmux(repo)


def _iso(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _owner(cwd: str, repos: list[str]) -> str | None:
    for repo in repos:
        if cwd == repo or cwd.startswith(repo + "/"):
            return repo
    return None


def _working_workspaces(tree) -> set[str]:
    """Ids of workspaces with a terminal tab whose title shows Claude Code's
    working spinner, from `cmux --json tree --all` (all windows)."""
    working = set()
    for window in (tree or {}).get("windows") or []:
        for ws in window.get("workspaces") or []:
            for pane in ws.get("panes") or []:
                for surface in pane.get("surfaces") or []:
                    title = surface.get("title") or ""
                    if surface.get("type") == "terminal" and title[:1] in CLAUDE_SPINNER:
                        working.add(ws.get("id"))
    return working


def _cmux_agent_states(repos: list[str]) -> dict[str, AgentState]:
    """repo -> AgentState, from its cmux workspaces (cwd at or under the repo).
    Running wins: a workspace with a tab whose title shows Claude Code's
    working spinner (CLAUDE_SPINNER). Otherwise waiting: the newest "Claude
    is waiting for your input" notification is newer than the last prompt
    submitted there, i.e. Claude asked and you haven't replied. cmux keeps
    notifications after they're read, so read state doesn't count.
    Off the main thread only: three cmux calls."""
    try:
        workspaces = json.loads(_cmux("rpc", "workspace.list").stdout).get("workspaces", [])
        notes = json.loads(_cmux("--json", "list-notifications").stdout)
        working = _working_workspaces(json.loads(_cmux("--json", "tree", "--all").stdout))
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, AttributeError):
        return {}
    newest: dict[str, tuple[datetime, str]] = {}  # workspace -> (when, notification id)
    for note in notes if isinstance(notes, list) else []:
        at = _iso(note.get("created_at"))
        ws_id = note.get("workspace_id")
        if at and ws_id and CMUX_WAITING_BODY in (note.get("body") or ""):
            if ws_id not in newest or at > newest[ws_id][0]:
                newest[ws_id] = (at, note.get("id"))

    owned = [
        (ws, repo)
        for ws in workspaces
        if ws.get("id") and (repo := _owner(ws.get("current_directory") or "", repos))
    ]
    states: dict[str, AgentState] = {}
    for ws, repo in owned:
        if ws["id"] in working:
            if states.get(repo, AgentState("", "")).state != "running":
                states[repo] = AgentState("running", ws["id"])
            continue
        asked = newest.get(ws["id"])
        replied = _iso(ws.get("latest_submitted_at"))
        if asked and (replied is None or asked[0] > replied) and repo not in states:
            states[repo] = AgentState("waiting", ws["id"], asked[1])
    return states


def relevant_event(line: str) -> bool:
    """Does this `cmux events` frame possibly change running/waiting?"""
    try:
        frame = json.loads(line)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(frame, dict)
        and frame.get("type") == "event"
        and frame.get("category") in CMUX_EVENT_CATEGORIES
        and frame.get("name") not in _IGNORED_EVENTS
    )


def watch_events(on_change: Callable[[], None]) -> None:
    """Blocking: stream `cmux events` and call on_change() for each event that
    may change agent state. Returns when the stream ends (cmux missing or the
    process died); the caller restarts it. Heartbeats are left on so the
    child exits on a broken pipe soon after this process does."""
    categories = [arg for c in CMUX_EVENT_CATEGORIES for arg in ("--category", c)]
    proc = subprocess.Popen(
        [CMUX_BIN, "events", "--reconnect", "--no-ack", *categories],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    atexit.register(proc.kill)
    try:
        for line in proc.stdout:
            if relevant_event(line):
                on_change()
    finally:
        proc.kill()
