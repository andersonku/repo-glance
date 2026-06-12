"""All app logic for repo-glance. Reloaded by main.py on every refresh,
so edits to this file take effect without restarting the app."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import rumps
from AppKit import NSAttributedString, NSFont, NSFontAttributeName

CONFIG_PATH = Path.home() / ".config" / "repo-glance" / "config.toml"

DEFAULTS = {
    "scan_dirs": ["~/dev", "~/dev2"],
    "repo_pattern": r"playmaker\d*",
    "refresh_seconds": 60,
    "title": "RG",
    "sort": "name",
}

SAMPLE_CONFIG = r'''# repo-glance configuration.
# Changes are picked up on the next refresh — no restart needed.

# Folders to scan for repo checkouts. "~" and $ENV_VARS are expanded.
scan_dirs = ["~/dev", "~/dev2"]

# A directory is shown if its name FULLY matches any of these regexes.
# A single string also works: repo_pattern = "playmaker\\d*"
repo_pattern = ["playmaker\\d*", "fastbreak\\d*"]

# Refresh interval, in seconds.
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


def discover_repos() -> list[str]:
    """Scan the configured dirs for repos whose name matches the configured pattern."""
    repos: list[str] = []
    for root in CONFIG.scan_dirs:
        if not root.is_dir():
            continue
        matches = [
            p for p in root.iterdir() if p.is_dir() and CONFIG.repo_pattern.match(p.name)
        ]
        repos.extend(str(p) for p in sorted(matches, key=lambda p: p.name))
    if CONFIG.sort in ("oldest", "newest"):
        repos.sort(key=_last_commit_epoch, reverse=CONFIG.sort == "newest")
    return repos


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


def refresh(app: rumps.App, also_print: bool) -> None:
    """Reload config, update the menu-bar app, and optionally print the CLI table.

    Called by the launcher on every tick, after this module has been reloaded.
    """
    global CONFIG
    CONFIG = load_config()
    app.title = CONFIG.title
    if app._timer.interval != CONFIG.refresh_seconds:
        app._timer.stop()
        app._timer.interval = CONFIG.refresh_seconds
        app._timer.start()
    _build_menu(app)
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
    app.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))


def _on_repo_click(repo: str, _: rumps.MenuItem) -> None:
    open_in_cmux(repo)


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
        f"(refreshing every {interval}s — Ctrl-C to exit)"
    )
    sys.stdout.flush()


def cli_tick() -> int:
    """Reload config and print the CLI table once; return the refresh interval."""
    global CONFIG
    CONFIG = load_config()
    _print_cli_table(CONFIG.refresh_seconds)
    return CONFIG.refresh_seconds
