"""All app logic for repo-glance. Reloaded by main.py on every tick,
so edits to this file take effect without restarting the app."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import rumps
from AppKit import NSAttributedString, NSFont, NSFontAttributeName

CONFIG_PATH = Path.home() / ".config" / "repo-glance" / "config.toml"

# How often the launcher ticks. Each tick is a cheap stat-based change check;
# a full refresh runs only on change or every refresh_seconds.
TICK_SECONDS = 2

DEFAULTS = {
    "scan_dirs": ["~/dev", "~/dev2"],
    "repo_pattern": r"playmaker\d*",
    "refresh_seconds": 60,
    "title": "RG",
    "sort": "name",
}

SAMPLE_CONFIG = r'''# repo-glance configuration.
# Changes are picked up within a couple of seconds — no restart needed.

# Folders to scan for repo checkouts. "~" and $ENV_VARS are expanded.
scan_dirs = ["~/dev", "~/dev2"]

# A directory is shown if its name FULLY matches any of these regexes.
# A single string also works: repo_pattern = "playmaker\\d*"
repo_pattern = ["playmaker\\d*", "fastbreak\\d*"]

# Seconds between unconditional full refreshes. Git changes (commits, branch
# switches, staging) and config edits are detected within ~2s regardless; this
# interval is the backstop that also catches unstaged working-tree edits.
refresh_seconds = 60

# Menu-bar title.
title = "RG"

# Order repos are listed in: "name" (default, grouped by scan dir),
# "oldest" (oldest last commit first), or "newest" (most recent first).
sort = "name"
'''


@dataclass(frozen=True)
class Config:
    scan_dirs: list[Path]
    repo_pattern: re.Pattern
    refresh_seconds: int
    title: str
    sort: str


def _write_sample_config() -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(SAMPLE_CONFIG)
    except OSError:
        pass


def load_config() -> Config:
    """Load config from CONFIG_PATH, falling back to defaults for missing keys."""
    raw = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("rb") as f:
                raw.update(tomllib.load(f))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"repo-glance: ignoring bad config: {exc}", file=sys.stderr)
    else:
        _write_sample_config()
    scan_dirs = [
        Path(os.path.expandvars(os.path.expanduser(d))) for d in raw["scan_dirs"]
    ]
    patterns = raw["repo_pattern"]
    if isinstance(patterns, str):
        patterns = [patterns]
    combined = "|".join(f"(?:{p})" for p in patterns)
    return Config(
        scan_dirs=scan_dirs,
        repo_pattern=re.compile(f"^(?:{combined})$"),
        refresh_seconds=int(raw["refresh_seconds"]),
        title=str(raw["title"]),
        sort=str(raw["sort"]),
    )


CONFIG = load_config()


def _last_commit_epoch(repo: str) -> int:
    out = _git(repo, "log", "-1", "--format=%ct")
    return int(out) if out.isdigit() else 0


def _candidate_repos() -> list[str]:
    """Scan the configured dirs for repos whose name matches the configured
    pattern. Stat-only (no git calls), so it is cheap enough to run every tick."""
    repos: list[str] = []
    for root in CONFIG.scan_dirs:
        if not root.is_dir():
            continue
        matches = [
            p for p in root.iterdir() if p.is_dir() and CONFIG.repo_pattern.match(p.name)
        ]
        repos.extend(str(p) for p in sorted(matches, key=lambda p: p.name))
    return repos


def discover_repos() -> list[str]:
    repos = _candidate_repos()
    if CONFIG.sort in ("oldest", "newest"):
        repos.sort(key=_last_commit_epoch, reverse=CONFIG.sort == "newest")
    return repos


# Files whose mtime reflects the git state we display: HEAD and logs/HEAD for
# branch switches and commits, index for staging, packed-refs for gc/fetch.
_FINGERPRINT_GIT_FILES = ("HEAD", "index", "packed-refs", "logs/HEAD")


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _fingerprint() -> tuple:
    """Stat-based snapshot of everything that should trigger a refresh: this
    file (hot reload), the config file, and each repo's git state. Unstaged
    working-tree edits don't touch any of these — the periodic full refresh
    picks those up."""
    entries: list = [_mtime(Path(__file__)), _mtime(CONFIG_PATH)]
    for repo in _candidate_repos():
        entries.append(repo)
        git_dir = Path(repo, ".git")
        entries.extend(_mtime(git_dir / name) for name in _FINGERPRINT_GIT_FILES)
    return tuple(entries)


CMUX_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"
CMUX_BUNDLE_ID = "com.cmuxterm.app"


def _cmux(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [CMUX_BIN, *args], capture_output=True, text=True, timeout=timeout
    )


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
            _cmux(repo)  # opens the dir in a new workspace, launching cmux if needed
    except (subprocess.SubprocessError, OSError):
        return
    subprocess.run(["open", "-b", CMUX_BUNDLE_ID], capture_output=True)


BRANCH_MAX = 40

NAME_W = 12
BRANCH_W = BRANCH_MAX
LAST_W = 12
COUNT_W = 3

_MONO_FONT = NSFont.monospacedSystemFontOfSize_weight_(13.0, 0.0)


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _relative_days(iso_date: str) -> str:
    days = (date.today() - date.fromisoformat(iso_date)).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def repo_summary(repo: str) -> str:
    name = Path(repo).name
    if not Path(repo, ".git").exists():
        return f"{name:<{NAME_W}}  (not a git repo)"
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if len(branch) > BRANCH_MAX:
        branch = branch[: BRANCH_MAX - 3] + "..."
    last_iso = _git(repo, "log", "-1", "--format=%cd", "--date=short")
    last = _relative_days(last_iso) if last_iso else "-"
    porcelain = _git(repo, "status", "--porcelain")
    uncommitted = len(porcelain.splitlines()) if porcelain else 0
    return (
        f"{name:<{NAME_W}}  "
        f"{branch:<{BRANCH_W}}  "
        f"{last:<{LAST_W}}  "
        f"{uncommitted:>{COUNT_W}} uncommitted"
    )


def _mono_item(text: str, callback=None) -> rumps.MenuItem:
    item = rumps.MenuItem(text, callback=callback)
    attributed = NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: _MONO_FONT}
    )
    item._menuitem.setAttributedTitle_(attributed)
    return item


def tick(app: rumps.App, also_print: bool) -> None:
    """Called by the launcher every TICK_SECONDS, after this module has been
    reloaded. Runs a full refresh only when the fingerprint changed or
    refresh_seconds has elapsed since the last one."""
    if _fingerprint() == getattr(app, "_rg_fingerprint", None) and (
        time.monotonic() - getattr(app, "_rg_last_refresh", 0.0)
        < CONFIG.refresh_seconds
    ):
        return
    refresh(app, also_print)


def refresh(app: rumps.App, also_print: bool) -> None:
    """Reload config, update the menu-bar app, and optionally print the CLI table.

    Unconditional — used at startup, by the Refresh-now menu item, and by
    tick() when a change is detected.
    """
    global CONFIG
    CONFIG = load_config()
    app.title = CONFIG.title
    _build_menu(app)
    # Snapshot AFTER the git calls above: `git status` may itself rewrite
    # .git/index, which must not register as a new change on the next tick.
    # State lives on the app object because module globals are wiped by reload.
    app._rg_fingerprint = _fingerprint()
    app._rg_last_refresh = time.monotonic()
    if also_print:
        _print_cli_table(CONFIG.refresh_seconds)


def _build_menu(app: rumps.App) -> None:
    app.menu.clear()
    for repo in discover_repos():
        app.menu.add(
            _mono_item(
                repo_summary(repo),
                callback=functools.partial(_on_repo_click, repo),
            )
        )
    app.menu.add(rumps.separator)
    app.menu.add(
        rumps.MenuItem(f"Refreshed {datetime.now().strftime('%H:%M:%S')}")
    )
    app.menu.add(rumps.MenuItem("Refresh now", callback=app._on_refresh))
    app.menu.add(rumps.MenuItem("Pull & restart", callback=_on_pull_restart))
    app.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))


def _on_repo_click(repo: str, _: rumps.MenuItem) -> None:
    open_in_cmux(repo)


def _on_pull_restart(_: rumps.MenuItem) -> None:
    """Pull the latest repo-glance code and restart the app.

    A full restart (rather than a reload) is needed because a pull can
    change main.py, which only takes effect in a new process. The restart
    spawns a fresh process and quits this one: exec-ing in place would
    reuse the PID, leaving the new NSApplication with a stale window-server
    registration and a dead status item.
    """
    app_repo = Path(__file__).resolve().parent
    result = subprocess.run(
        ["git", "-C", str(app_repo), "pull", "--ff-only"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        rumps.alert(
            "repo-glance update failed",
            result.stderr.strip() or result.stdout.strip(),
        )
        return
    subprocess.Popen(
        [sys.executable, str(app_repo / "main.py"), *sys.argv[1:]],
        start_new_session=True,
    )
    rumps.quit_application()


def _header() -> str:
    return (
        f"{'REPO':<{NAME_W}}  "
        f"{'BRANCH':<{BRANCH_W}}  "
        f"{'LAST COMMIT':<{LAST_W}}  "
        f"UNCOMMITTED"
    )


def _print_cli_table(interval: int = CONFIG.refresh_seconds) -> None:
    print("\033[2J\033[H", end="")
    print(_header())
    print("-" * len(_header()))
    for repo in discover_repos():
        print(repo_summary(repo))
    print()
    print(
        f"Last refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
        f"(refresh on change, full refresh every {interval}s — Ctrl-C to exit)"
    )
    sys.stdout.flush()


def cli_tick(state: dict) -> int:
    """Print the CLI table when something changed or refresh_seconds elapsed;
    return the seconds to sleep. `state` is owned by the launcher so it
    survives reloads of this module."""
    global CONFIG
    CONFIG = load_config()
    if _fingerprint() != state.get("fingerprint") or (
        time.monotonic() - state.get("last_refresh", 0.0) >= CONFIG.refresh_seconds
    ):
        _print_cli_table(CONFIG.refresh_seconds)
        # Snapshot after printing: the git calls may rewrite .git/index.
        state["fingerprint"] = _fingerprint()
        state["last_refresh"] = time.monotonic()
    return TICK_SECONDS
