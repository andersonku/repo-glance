"""WIP.md (for people and agents to read) and WIP.json (for agents to parse):
the menu's contents, untruncated, rewritten on every refresh."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from . import config as cfg
from .cmux import AgentState
from .fmt import _agent_mark, _relative_days, _sync_lines
from .prs import _ci_state, _pr_extra, _ticket_for
from .snapshot import Snapshot, claude_usages

WIP_JSON_VERSION = 1


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _wip_usage_line(name: str, percent: int, resets, read_at, stale) -> str:
    line = f"- {name}: {percent}% used"
    if resets is not None:
        line += f", resets {resets.strftime('%Y-%m-%d %H:%M')}"
    if stale:
        line += f" (stale, as of {read_at.strftime('%Y-%m-%d %H:%M')})"
    return line


def _short_path(repo: str) -> str:
    home = str(Path.home())
    return repo.replace(home, "~", 1) if repo.startswith(home) else repo


def _wip_markdown(snap: Snapshot, agents: dict[str, AgentState]) -> str:
    """The menu's contents as Markdown, untruncated, for other agents."""
    out = [
        "# WIP: local repo checkouts",
        "",
        f"Written by repo-glance at {snap.collected_at.strftime('%Y-%m-%d %H:%M:%S')}; "
        "rewritten on every refresh, so don't edit it. WIP.json next to this file "
        "has the same data as JSON.",
        "",
    ]
    out += [
        "Markers before a repo name: `◐` a cmux agent there is running; `✳` Claude "
        "there is waiting for the user's input; `★` the user marked it important.",
        "",
    ]
    if cfg.CONFIG.show_usage:
        out.append("## Usage")
        out.append("")
        for label, reading in claude_usages(snap):
            out.append(_wip_usage_line(f"{label} (5-hour session)", *reading)
                       if reading else f"- {label}: usage unavailable")
        if snap.codex:
            out.append(_wip_usage_line("Codex", *snap.codex))
        out.append("")
    if snap.sync:
        out += ["## Behind main (Node 24 upgrade)", ""]
        for label, (behind, ahead), extra in _sync_lines(snap.sync):
            line = f"- {label}: {behind} behind main, {ahead} ahead"
            out.append(line + (f", {extra}" if extra else ""))
        out.append("")
    out += [
        "## Repos",
        "",
        "| Repo | Path | Branch | Last commit | Uncommitted | PR | Ticket |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for info in snap.infos:
        path = _short_path(info.repo)
        name = Path(info.repo).name
        mark = _agent_mark(agents.get(info.repo))
        marks = (f"{mark} " if mark else "") + (
            "★ " if name in cfg.CONFIG.important else ""
        )
        name = marks + name
        if not info.is_git:
            out.append(f"| {name} | {path} | (not a git repo) | | | | |")
            continue
        last = f"{info.last_iso} ({_relative_days(info.last_iso)})" if info.last_iso else "-"
        pr = snap.prs.get(info.repo) if cfg.CONFIG.show_pr else None
        pr_cell = ""
        if pr:
            pr_cell = f"[#{pr['number']} {pr.get('title', '')}]({pr['url']})"
            extra = _pr_extra(pr)
            if extra:
                pr_cell += f" ({extra})"
        ticket = _ticket_for(info.branch, pr) or ""
        cells = (name, path, info.branch, last, info.uncommitted, pr_cell, ticket)
        out.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    return "\n".join(out) + "\n"


def _iso_or_none(when: datetime | None) -> str | None:
    return when.isoformat(timespec="seconds") if when else None


def _usage_json(reading) -> dict | None:
    if not reading:
        return None
    percent, resets, read_at, stale = reading
    return {
        "percent_used": percent,
        "resets_at": _iso_or_none(resets),
        "read_at": _iso_or_none(read_at),
        "stale": stale,
    }


def _wip_data(snap: Snapshot, agents: dict[str, AgentState]) -> dict:
    """The same content as _wip_markdown, as plain JSON-ready data."""
    repos = []
    for info in snap.infos:
        name = Path(info.repo).name
        pr = snap.prs.get(info.repo) if cfg.CONFIG.show_pr else None
        agent = agents.get(info.repo)
        repos.append({
            "name": name,
            "path": info.repo,
            "is_git": info.is_git,
            "branch": info.branch or None,
            "last_commit_date": info.last_iso or None,
            "uncommitted": info.uncommitted,
            "important": name in cfg.CONFIG.important,
            # "running", "waiting" (Claude asked for input), or null.
            "agent": agent.state if agent else None,
            "pr": {
                "number": pr.get("number"),
                "title": pr.get("title"),
                "url": pr.get("url"),
                "state": pr.get("state"),
                "draft": bool(pr.get("isDraft")),
                "ci": _ci_state(pr),
                "review": pr.get("reviewDecision") or None,
                "labels": _pr_extra(pr) or None,
            } if pr else None,
            "ticket": _ticket_for(info.branch, pr) if info.is_git else None,
        })
    data = {
        "version": WIP_JSON_VERSION,
        "generated_at": _iso_or_none(snap.collected_at.astimezone()),
        "repos": repos,
    }
    if cfg.CONFIG.show_usage:
        accounts = claude_usages(snap)
        data["usage"] = {
            # "claude" is the first configured account (compatible with
            # single-account readers); "claude_accounts" has every account.
            "claude": _usage_json(accounts[0][1]) if accounts else None,
            "claude_accounts": [
                {"label": label, "usage": _usage_json(reading)} for label, reading in accounts
            ],
            "codex": _usage_json(snap.codex),
        }
    if snap.sync:
        data["behind_main"] = [
            {"label": label, "behind": behind, "ahead": ahead, "extra": extra or None}
            for label, (behind, ahead), extra in _sync_lines(snap.sync)
        ]
    return data


def _atomic_write(path: Path, text: str) -> None:
    # A per-thread temp name keeps the collector and the cmux poller apart.
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _write_wip(snap: Snapshot | None, agents: dict[str, AgentState]) -> None:
    """Write WIP.md and WIP.json. Worker/poller thread. Atomic replace, so a
    reader never sees a half-written file."""
    if snap is None or not cfg.CONFIG.wip_file:
        return
    path = Path(cfg.CONFIG.wip_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, _wip_markdown(snap, agents))
    _atomic_write(path.with_suffix(".json"), json.dumps(_wip_data(snap, agents), indent=2) + "\n")
