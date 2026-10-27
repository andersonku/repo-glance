"""Remote-tracking counts: the TEMPORARY behind-main section and how far this
app's own checkout is behind its upstream (for the "Open GitHub" footer)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .config import APP_REPO, FETCH_SECONDS
from .prs import _ci_state, _pr_lookup
from .repos import _git

# TEMPORARY, hard-coded: track the Node 24 upgrade (PR #10405) against main —
# both the remote PR branch and the local playmaker9 checkout of it. Remove
# once that project lands.
SYNC_REPO = str(Path.home() / "dev2" / "playmaker9")
SYNC_BRANCH = "feat/GO-0-node-24"
SYNC_PR_URL = "https://github.com/GameOnSports/playmaker/pull/10405"


def _fetch_refs(repo: str, *refs: str) -> None:
    """Refresh origin/<ref> for the given refs only — much cheaper than a full
    fetch on a big repo. Never prompts for credentials (would hang the refresh)."""
    try:
        subprocess.run(
            ["git", "-C", repo, "fetch", "--quiet", "origin", *refs],
            capture_output=True,
            timeout=20,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        pass


def _behind_ahead(repo: str, base: str, head: str) -> tuple[int, int]:
    """(commits on base missing from head, commits on head missing from base)."""
    out = _git(repo, "rev-list", "--left-right", "--count", f"{base}...{head}")
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return 0, 0
    return int(parts[0]), int(parts[1])


def _sync_data(app) -> tuple[str | None, tuple[int, int], tuple[int, int]] | None:
    """(CI state, remote (behind, ahead), local (behind, ahead)) vs origin/main.
    Worker thread. The fetch and PR lookup are rate-limited (timestamp on the
    app object survives reloads); the rev-lists are local and run every
    refresh, so the counts self-correct right after a rebase/push."""
    if not Path(SYNC_REPO, ".git").exists():
        return None
    now = time.monotonic()
    last = getattr(app, "_rg_sync_fetched", None)
    if last is None or now - last >= FETCH_SECONDS:
        _fetch_refs(SYNC_REPO, "main", SYNC_BRANCH)
        # Keep the last good PR reading if gh fails this round.
        app._rg_sync_pr = _pr_lookup(SYNC_REPO, SYNC_PR_URL) or getattr(
            app, "_rg_sync_pr", None
        )
        app._rg_sync_fetched = now
    pr = getattr(app, "_rg_sync_pr", None)
    return (
        _ci_state(pr) if pr else None,
        _behind_ahead(SYNC_REPO, "origin/main", f"origin/{SYNC_BRANCH}"),
        _behind_ahead(SYNC_REPO, "origin/main", "HEAD"),
    )


def _commits_behind(app) -> int:
    """How many commits this checkout is behind upstream. The fetch is
    rate-limited (timestamp lives on the app object so it survives reloads);
    the rev-list itself is local and runs every refresh, so the count
    self-corrects right after a pull."""
    app_repo = str(APP_REPO)
    last = getattr(app, "_rg_last_fetch", None)
    if last is None or time.monotonic() - last >= FETCH_SECONDS:
        try:
            subprocess.run(
                ["git", "-C", app_repo, "fetch", "--quiet"],
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass
        app._rg_last_fetch = time.monotonic()
    out = _git(app_repo, "rev-list", "--count", "HEAD..@{u}")
    return int(out) if out.isdigit() else 0
