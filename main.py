"""Menu bar app showing status of each playmaker checkout."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import rumps
from AppKit import NSAttributedString, NSFont, NSFontAttributeName

DEV_DIRS = [
    Path.home() / "dev",
    Path.home() / "dev2",
]
_REPO_RE = re.compile(r"^playmaker\d*$")
REFRESH_SECONDS = 60


def discover_repos() -> list[str]:
    """Scan the dev dirs for playmaker checkouts (playmaker, playmaker2, ...)."""
    repos: list[str] = []
    for root in DEV_DIRS:
        if not root.is_dir():
            continue
        matches = [p for p in root.iterdir() if p.is_dir() and _REPO_RE.match(p.name)]
        repos.extend(str(p) for p in sorted(matches, key=lambda p: p.name))
    return repos
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


class PlaymakerStatusApp(rumps.App):
    def __init__(self, also_print: bool = True) -> None:
        super().__init__("PM", quit_button=None)
        self._also_print = also_print
        self._refresh()
        self._timer = rumps.Timer(self._on_tick, REFRESH_SECONDS)
        self._timer.start()

    def _refresh(self) -> None:
        self._build_menu()
        if self._also_print:
            _print_cli_table()

    def _build_menu(self) -> None:
        self.menu.clear()
        for repo in discover_repos():
            self.menu.add(_mono_item(repo_summary(repo)))
        self.menu.add(rumps.separator)
        self.menu.add(
            rumps.MenuItem(f"Refreshed {datetime.now().strftime('%H:%M:%S')}")
        )
        self.menu.add(rumps.MenuItem("Refresh now", callback=self._on_refresh))
        self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

    def _on_tick(self, _: rumps.Timer) -> None:
        self._refresh()

    def _on_refresh(self, _: rumps.MenuItem) -> None:
        self._refresh()


def _header() -> str:
    return (
        f"{'REPO':<{NAME_W}}  "
        f"{'BRANCH':<{BRANCH_W}}  "
        f"{'LAST COMMIT':<{LAST_W}}  "
        f"UNCOMMITTED"
    )


def _print_cli_table(interval: int = REFRESH_SECONDS) -> None:
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


def cli_loop(interval: int = REFRESH_SECONDS) -> None:
    try:
        while True:
            _print_cli_table(interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        cli_loop()
    elif "--no-cli" in sys.argv:
        PlaymakerStatusApp(also_print=False).run()
    else:
        PlaymakerStatusApp(also_print=True).run()
