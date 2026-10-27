"""The data the menu, the CLI table, and WIP.md/WIP.json are rendered from."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .repos import RepoInfo


@dataclass
class Snapshot:
    """Everything the menu is rendered from, gathered off the main thread."""
    infos: list[RepoInfo]
    prs: dict[str, dict | None]
    usage: list[tuple[str, tuple | None]]  # (label, reading) per Claude account
    codex: tuple | None
    sync: tuple | None
    app_behind: int
    fingerprint: tuple
    collected_at: datetime
    changed_during: bool  # a commit/checkout landed mid-collection; recollect


def claude_usages(snap: Snapshot) -> list[tuple[str, tuple | None]]:
    """snap.usage, tolerating the single-reading shape an older build's
    snapshot (still on the app object across a reload) used."""
    usage = snap.usage
    if usage is None or isinstance(usage, tuple):
        return [("Claude", usage)]
    return list(usage)
