"""Repo discovery, per-repo git status, and the stat-based change fingerprint."""

from __future__ import annotations

import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg


@dataclass
class RepoInfo:
    repo: str
    branch: str
    epoch: int
    last_iso: str
    uncommitted: int
    is_git: bool


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _shown(name: str) -> bool:
    config = cfg.CONFIG
    if not config.repo_pattern or not config.repo_pattern.match(name):
        return False
    return not (config.hide_pattern and config.hide_pattern.match(name))


def _natural_key(name: str) -> list:
    """Sort key that orders digit runs numerically: playmaker9 < playmaker10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _candidate_repos() -> list[str]:
    """Scan configured dirs for repos matching the pattern. Stat-only, no git calls."""
    repos: list[str] = []
    for root in cfg.CONFIG.scan_dirs:
        if not root.is_dir():
            continue
        matches = [p for p in root.iterdir() if p.is_dir() and _shown(p.name)]
        repos.extend(str(p) for p in sorted(matches, key=lambda p: _natural_key(p.name)))
    return repos


def _fetch_repo_info(repo: str) -> RepoInfo:
    """Fetch all display data for one repo in 3 git calls (was 4, epoch+date combined)."""
    if not Path(repo, ".git").exists():
        return RepoInfo(repo=repo, branch="", epoch=0, last_iso="", uncommitted=0, is_git=False)
    log_out = _git(repo, "log", "-1", "--format=%ct\n%cd", "--date=short")
    lines = log_out.splitlines()
    epoch = int(lines[0]) if lines and lines[0].isdigit() else 0
    last_iso = lines[1] if len(lines) > 1 else ""
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(repo, "status", "--porcelain")
    uncommitted = len(porcelain.splitlines()) if porcelain else 0
    return RepoInfo(repo=repo, branch=branch, epoch=epoch, last_iso=last_iso,
                    uncommitted=uncommitted, is_git=True)


def discover_repos() -> list[RepoInfo]:
    candidates = _candidate_repos()
    with ThreadPoolExecutor(max_workers=16) as ex:
        infos = list(ex.map(_fetch_repo_info, candidates))
    if cfg.CONFIG.active_days > 0:
        cutoff = time.time() - cfg.CONFIG.active_days * 86400
        infos = [i for i in infos if i.epoch >= cutoff]
    if cfg.CONFIG.sort == "newest":
        infos.sort(key=lambda i: i.epoch, reverse=True)
    elif cfg.CONFIG.sort == "oldest":
        infos.sort(key=lambda i: i.epoch)
    else:
        infos.sort(key=lambda i: _natural_key(Path(i.repo).name))
    return infos


# Files whose mtime reflects the git state we display: HEAD and logs/HEAD for
# branch switches and commits, index for staging, packed-refs for gc/fetch.
_FINGERPRINT_GIT_FILES = ("HEAD", "index", "packed-refs", "logs/HEAD")


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _code_files() -> list[Path]:
    """The hot-reloaded sources: logic.py and every rg module."""
    return [cfg.APP_REPO / "logic.py", *sorted((cfg.APP_REPO / "rg").glob("*.py"))]


def _fingerprint() -> tuple:
    """Stat-based snapshot of everything that should trigger a refresh: the
    app's code (hot reload), config and state files, and each repo's git
    state. Unstaged working-tree edits don't touch any of these — the periodic
    full refresh picks those up."""
    entries: list = [_mtime(p) for p in _code_files()]
    entries += [_mtime(cfg.CONFIG_PATH), _mtime(cfg.STATE_PATH)]
    for repo in _candidate_repos():
        entries.append(repo)
        git_dir = Path(repo, ".git")
        entries.extend(_mtime(git_dir / name) for name in _FINGERPRINT_GIT_FILES)
    return tuple(entries)


def _changed_during(before: tuple, after: tuple) -> bool:
    """Did anything other than .git/index change between two fingerprints?
    Used to spot a commit/checkout that landed while a collection was running.
    Index changes are ignored because the collection's own `git status` may
    rewrite it; a staging-only change is caught by the periodic refresh."""
    if len(before) != len(after):
        return True
    header = len(_code_files()) + 2  # code files, config, state
    stride = len(_FINGERPRINT_GIT_FILES) + 1  # + the repo path entry
    index_pos = _FINGERPRINT_GIT_FILES.index("index") + 1
    return any(
        a != b and not (i >= header and (i - header) % stride == index_pos)
        for i, (a, b) in enumerate(zip(before, after))
    )


def default_branch(repo: str) -> str:
    """origin's default branch (from origin/HEAD), else main or master."""
    ref = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if ref.startswith("origin/"):
        return ref.removeprefix("origin/")
    for name in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return "main"


def switch_to_default_and_pull(repo: str) -> str | None:
    """For a checkout whose PR merged: switch to the default branch and
    fast-forward it. Refuses on uncommitted changes. Returns an error message,
    or None on success. Slow (network): call off the main thread."""
    if _git(repo, "status", "--porcelain"):
        return "has uncommitted changes; commit or stash them first."
    branch = default_branch(repo)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for args, timeout in ((["switch", branch], 20), (["pull", "--ff-only"], 120)):
        try:
            result = subprocess.run(
                ["git", "-C", repo, *args], capture_output=True, text=True,
                timeout=timeout, env=env,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return f"git {args[0]} failed: {exc}"
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
            return f"git {args[0]} failed: {detail[-1] if detail else 'unknown error'}"
    return None
