"""Pull requests via the gh CLI, CI/review labels, and Jira ticket keys."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from .config import FETCH_SECONDS
from .repos import RepoInfo

# gh lives in /opt/homebrew/bin, which isn't on PATH when launched as an app.
GH_BIN = shutil.which("gh") or "/opt/homebrew/bin/gh"

PR_TITLE_W = 50  # PR titles longer than this are truncated with "..."


def _pr_lookup(repo: str, selector: str | None = None) -> dict | None:
    """PR for the repo's current branch via `gh pr view` (resolves the branch
    itself), or for an explicit number/branch/URL `selector`. None if there is
    no PR, gh is missing, or gh isn't authed."""
    try:
        result = subprocess.run(
            [GH_BIN, "pr", "view", *([selector] if selector else []), "--json",
             "number,url,title,state,isDraft,reviewDecision,statusCheckRollup"],
            cwd=repo, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None


_CI_FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE"}
_CI_DONE = _CI_FAILED | {"SUCCESS", "NEUTRAL", "SKIPPED"}


def _ci_state(pr: dict) -> str | None:
    """"failed" / "running" / "passed", or None when the PR has no checks."""
    # Rollup entries are CheckRuns (conclusion, empty until completed) or
    # commit StatusContexts (state).
    results = {
        c.get("conclusion") or c.get("state") or "PENDING"
        for c in pr.get("statusCheckRollup") or []
    }
    if not results:
        return None
    if results & _CI_FAILED:
        return "failed"
    if results - _CI_DONE:
        return "running"
    return "passed"


def _pr_extra(pr: dict) -> str:
    """Noteworthy PR state as short labels ("CI failed, Approved"). Quiet
    states (open, CI green, no review yet) produce nothing."""
    # Merged PRs report no usable CI status, and review state no longer
    # matters — "Merged" says it all.
    if pr.get("state") == "MERGED":
        return "Merged"
    labels = []
    if pr.get("state") == "CLOSED":
        labels.append("Closed")
    if pr.get("isDraft"):
        labels.append("Draft")
    ci = _ci_state(pr)
    if ci in ("failed", "running"):
        labels.append(f"CI {ci}")
    decision = pr.get("reviewDecision")
    if decision == "APPROVED":
        labels.append("Approved")
    elif decision == "CHANGES_REQUESTED":
        labels.append("Changes requested")
    return ", ".join(labels)


def _pr_map(app, infos: list[RepoInfo]) -> dict[str, dict | None]:
    """repo -> PR info for the branch it is on. Lookups hit the network, so
    results are cached by (repo, branch) on the app object (survives reloads)
    and re-checked at most every FETCH_SECONDS — which also picks up PRs
    opened after a branch was first seen."""
    cache = getattr(app, "_rg_pr_cache", None)
    if cache is None:
        cache = app._rg_pr_cache = {}
    now = time.monotonic()
    targets = [i for i in infos if i.is_git and i.branch]
    stale = [
        i for i in targets
        if (i.repo, i.branch) not in cache
        or now - cache[(i.repo, i.branch)][0] >= FETCH_SECONDS
    ]
    if stale:
        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(lambda i: _pr_lookup(i.repo), stale))
        for info, pr in zip(stale, results):
            cache[(info.repo, info.branch)] = (now, pr)
    return {i.repo: cache[(i.repo, i.branch)][1] for i in targets}


JIRA_BROWSE_URL = "https://gameonsport.atlassian.net/browse/"

_TICKET_RE = re.compile(r"\b(GO-\d+)\b", re.IGNORECASE)


def _ticket_for(branch: str, pr: dict | None) -> str | None:
    """Jira ticket key referenced by the branch name or the PR title."""
    for text in (branch, (pr or {}).get("title", "")):
        m = _TICKET_RE.search(text or "")
        if m:
            return m.group(1).upper()
    return None
