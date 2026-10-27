"""Plain-text formatting shared by the menu rows, the CLI table, and WIP.md."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from AppKit import NSColor

from . import config as cfg
from .cmux import AgentState
from .repos import RepoInfo

LAST_W = 7

# Marker shown before the repo name for each agent state, and its color.
AGENT_MARKS = {
    "running": ("◐", NSColor.systemGreenColor),
    "waiting": ("✳", NSColor.systemRedColor),
}


def _relative_days(iso_date: str) -> str:
    days = (date.today() - date.fromisoformat(iso_date)).days
    if days <= 0:
        return "Today"
    return f"{days}d"


def _name_width(infos: list[RepoInfo]) -> int:
    return max((len(Path(i.repo).name) for i in infos), default=12)


def _agent_mark(agent: AgentState | None) -> str:
    return AGENT_MARKS[agent.state][0] if agent and agent.state in AGENT_MARKS else ""


def _gutter(repo: str, agents: dict[str, AgentState] | None = None) -> str:
    """Marker columns before the name: the agent state (AGENT_MARKS) from
    cmux, and "★" when it's marked important. Each column only appears while
    at least one repo uses it."""
    out = ""
    if agents:
        mark = _agent_mark(agents.get(repo))
        out += f"{mark} " if mark else "  "
    if cfg.CONFIG.important:
        out += "★ " if Path(repo).name in cfg.CONFIG.important else "  "
    return out


def repo_summary(
    info: RepoInfo, name_w: int = 12, agents: dict[str, AgentState] | None = None
) -> str:
    name = Path(info.repo).name
    star = _gutter(info.repo, agents)
    if not info.is_git:
        return f"{star}{name:<{name_w}}  (not a git repo)"
    branch_w = cfg.CONFIG.branch_width
    branch = info.branch
    if len(branch) > branch_w:
        branch = branch[: branch_w - 3] + "..."
    last = _relative_days(info.last_iso) if info.last_iso else "-"
    dirty = f"~{info.uncommitted}" if info.uncommitted else ""
    return (
        f"{star}{name:<{name_w}}  "
        f"{branch:<{branch_w}}  "
        f"{last:<{LAST_W}}  "
        f"{dirty}"
    )


def _header(name_w: int = 12, agents: dict[str, AgentState] | None = None) -> str:
    return (
        f"{_gutter('', agents)}{'REPO':<{name_w}}  "
        f"{'BRANCH':<{cfg.CONFIG.branch_width}}  "
        f"{'AGE':<{LAST_W}}  "
        f"DIRTY"
    )


def _sync_lines(data) -> tuple:
    """(label, (behind, ahead), extra) for the remote PR branch and the local
    checkout."""
    ci, remote, local = data
    return (
        ("PR #10405 (remote)", remote, f"CI {ci}" if ci else ""),
        ("playmaker9 (local)", local, ""),
    )
