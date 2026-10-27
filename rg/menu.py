"""The dropdown: problems first, then usage, the behind-main section, repo
rows with their PR lines, and the footer. Main thread, pure rendering of a
Snapshot — no git, gh, or network calls here."""

from __future__ import annotations

import functools
import subprocess
import time
from datetime import datetime
from pathlib import Path

import objc
import rumps
from AppKit import (
    NSApplication,
    NSColor,
    NSEventTypeLeftMouseDown,
    NSEventTypeRightMouseDown,
    NSImage,
    NSObject,
    NSProcessInfo,
)

import rg

from . import config as cfg
from .config import APP_REPO, LOG_MAX_BYTES, LOG_PATH
from .fmt import _gutter, _name_width, _sync_lines, repo_summary
from .notices import add_notice, dismiss_notice, notices
from .prs import PR_TITLE_W, _pr_extra, _ticket_for
from .rows import (
    ROW_INSET,
    _mono_attr,
    _mono_item,
    _on_open_url,
    _on_row_select,
    _repo_row_item,
)
from .snapshot import Snapshot, claude_usages
from .sync import SYNC_PR_URL, SYNC_REPO
from .usage import USAGE_SETTINGS_URL, usage_summary

GITHUB_URL = "https://github.com/andersonku/repo-glance"
ICON_HEIGHT = 18  # standard menu-bar icon height in points
# A menu that opens this long after the click gets a line in LOG_PATH.
SLOW_OPEN_MS = 150


def _menu_delegate_class():
    """NSMenuDelegate for the dropdown, used to time click -> open. Methods
    are thin delegates (hot-reloadable); bump the suffix if the class body
    must change."""
    try:
        return objc.lookUpClass("RGMenuDelegate1")
    except objc.error:
        pass

    class RGMenuDelegate1(NSObject):
        def menuWillOpen_(self, _menu):
            _on_menu_will_open(self._rg_app)

    return RGMenuDelegate1


def install_menu_delegate(app) -> None:
    """Attach the delegate to the dropdown once (NSMenu holds delegates
    weakly, so the app keeps the reference)."""
    nsmenu = getattr(getattr(app, "menu", None), "_menu", None)
    if nsmenu is None or getattr(app, "_rg_menu_delegate", None) is not None:
        return
    delegate = _menu_delegate_class().alloc().init()
    delegate._rg_app = app
    nsmenu.setDelegate_(delegate)
    app._rg_menu_delegate = delegate


def _on_menu_will_open(app) -> None:
    """Main thread, as the dropdown opens: if it took SLOW_OPEN_MS or more
    since the click, log what the app was doing. Cheap when fast."""
    app._rg_menu_opens = getattr(app, "_rg_menu_opens", 0) + 1
    event = NSApplication.sharedApplication().currentEvent()
    if event is None or event.type() not in (NSEventTypeLeftMouseDown, NSEventTypeRightMouseDown):
        return
    latency_ms = (NSProcessInfo.processInfo().systemUptime() - event.timestamp()) * 1000
    if latency_ms >= SLOW_OPEN_MS:
        _log_line(f"menu opened {latency_ms:.0f} ms after the click  [{_busy_context(app)}]")


def _busy_context(app) -> str:
    """What could have been holding up the main thread, for the log."""
    now = time.monotonic()
    parts = []
    worker = getattr(app, "_rg_worker", None)
    if worker is not None and worker.is_alive():
        parts.append(f"refresh running for {now - getattr(app, '_rg_last_refresh', now):.1f}s")
    poller = getattr(app, "_rg_cmux_worker", None)
    if poller is not None and poller.is_alive():
        parts.append("cmux poll running")
    tick = getattr(app, "_rg_tick", None)
    if tick:
        parts.append(f"last tick {now - tick[0]:.1f}s ago took {tick[1]:.0f} ms")
    reload = getattr(rg, "last_reload", None)
    if reload:
        parts.append(f"last reload {now - reload[0]:.1f}s ago took {reload[1]:.0f} ms ({reload[2]} modules)")
    parts.append("App Nap off" if getattr(app, "_rg_activity", None) is not None else "App Nap allowed")
    parts.append(f"open #{getattr(app, '_rg_menu_opens', 0)}")
    return ", ".join(parts)


def _log_line(text: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".old"))
        with LOG_PATH.open("a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
    except OSError:
        pass


def _set_status_icon(app: rumps.App, path: str) -> bool:
    """Set the menu-bar icon, preserving aspect ratio and enabling template
    mode (so macOS recolors it for dark menu bars and the highlighted state).

    rumps' own `app.icon` setter forces every image to 20x20, distorting
    non-square icons, so build the NSImage here and hand it to the same
    internals the setter uses. Returns False if the image can't be loaded.
    """
    img = NSImage.alloc().initByReferencingFile_(path)
    if img is None or not img.isValid():
        return False
    size = img.size()
    img.setSize_((size.width * ICON_HEIGHT / size.height, ICON_HEIGHT))
    img.setTemplate_(True)
    app._icon = path
    app._icon_nsimage = img
    try:
        app._nsapp.setStatusBarIcon()
    except AttributeError:
        pass
    return True


def agent_states(app) -> dict:
    """repo -> cmux.AgentState, as last polled (see app._poll_cmux)."""
    return getattr(app, "_rg_agent_states", {})


def _open_in_editor(path: Path, _: rumps.MenuItem) -> None:
    if path == cfg.CONFIG_PATH and not path.exists():
        cfg._write_sample_config()
    subprocess.run(["open", "-t", str(path)], capture_output=True)


def _on_dismiss_notice(app, text: str, _: rumps.MenuItem) -> None:
    dismiss_notice(app, text)


def _add_problems(app: rumps.App) -> None:
    """Red rows for config/state errors (click opens the file) and notices
    (click dismisses). Nothing when all is well."""
    rows = [
        (f"⚠ {message}", functools.partial(_open_in_editor, path))
        for path, message in cfg.CONFIG.errors
    ] + [
        (f"⚠ {text}  (click to dismiss)", functools.partial(_on_dismiss_notice, app, text))
        for text in notices(app)
    ]
    for i, (text, callback) in enumerate(rows):
        app.menu[f"problem:{i}"] = _mono_item(
            text, callback=callback, color=NSColor.systemRedColor()
        )
    if rows:
        app.menu.add(rumps.separator)


def _add_sync_section(app: rumps.App, data) -> None:
    """Two lines: how far the remote PR branch and the local checkout are behind
    origin/main — the cue to rebase."""
    callbacks = (
        functools.partial(_on_open_url, SYNC_PR_URL),
        functools.partial(_on_row_select, SYNC_REPO),
    )
    for (label, (behind, ahead), extra), callback in zip(_sync_lines(data), callbacks):
        text = f"{label:<20}  {behind:>4} behind main"
        if ahead:
            text += f", {ahead} ahead"
        if extra:
            text += f"  [{extra}]"
        app.menu[f"sync:{label}"] = _mono_item(text, callback=callback)
    app.menu.add(rumps.separator)


def usage_line(snap: Snapshot) -> str:
    """The single usage row: one segment per Claude login, then Codex. Keep
    the labels short (claude_dirs table, codex_label) so it stays narrow."""
    segments = [
        usage_summary(label, *reading) if reading else f"{label} usage unavailable"
        for label, reading in claude_usages(snap)
    ]
    if snap.codex:
        segments.append(usage_summary(cfg.CONFIG.codex_label, *snap.codex))
    return "  │  ".join(segments)


def _build_menu(app: rumps.App, snap: Snapshot | None) -> None:
    """Main thread. Pure rendering of a snapshot — no git, gh, or network here.
    With no snapshot yet (startup), shows a placeholder with the footer."""
    app.menu.clear()
    _add_problems(app)
    if snap is None:
        app.menu.add(_mono_item("Loading…"))
        app.menu.add(rumps.separator)
        _add_footer(app, refreshed=None, app_behind=0)
        return
    if cfg.CONFIG.show_usage:
        app.menu.add(
            _mono_item(usage_line(snap), callback=functools.partial(_on_open_url, USAGE_SETTINGS_URL))
        )
        app.menu.add(rumps.separator)
    if snap.sync:
        _add_sync_section(app, snap.sync)
    name_w = _name_width(snap.infos)
    agents = agent_states(app)
    summaries = [(info, repo_summary(info, name_w, agents)) for info in snap.infos]
    row_w = max(
        (_mono_attr(s, NSColor.labelColor()).size().width for _, s in summaries),
        default=0.0,
    ) + 2 * ROW_INSET + 12  # extra room for the submenu arrow
    for info, summary in summaries:
        pr = snap.prs.get(info.repo) if cfg.CONFIG.show_pr else None
        app.menu[f"{info.repo}:row"] = _repo_row_item(
            app, info.repo, summary, row_w, _ticket_for(info.branch, pr), pr,
            agent=agents.get(info.repo),
        )
        if pr:
            # Explicit key: rumps keys items by title and silently drops
            # duplicates, which would hide the PR row of a second checkout
            # on the same branch.
            title = pr.get("title", "")
            if len(title) > PR_TITLE_W:
                title = title[: PR_TITLE_W - 3] + "..."
            extra = _pr_extra(pr)
            # Indent past the marker columns so ↳ stays under the repo name.
            indent = " " * (3 + len(_gutter(info.repo, agents)))
            app.menu[f"{info.repo}:pr"] = _mono_item(
                f"{indent}↳ PR #{pr['number']}  {title}" + (f"  [{extra}]" if extra else ""),
                callback=functools.partial(_on_open_url, pr["url"]),
            )
    app.menu.add(rumps.separator)
    _add_footer(app, refreshed=snap.collected_at, app_behind=snap.app_behind)


def _add_footer(app: rumps.App, refreshed: datetime | None, app_behind: int) -> None:
    stamp = refreshed.strftime("%H:%M:%S") if refreshed else "never"
    app.menu.add(rumps.MenuItem(f"Refreshed {stamp}"))
    app.menu.add(rumps.MenuItem("Refresh now", callback=app._on_refresh))
    app.menu.add(
        rumps.MenuItem(
            "Hide PRs" if cfg.CONFIG.show_pr else "Show PRs",
            callback=functools.partial(_on_toggle_pr, app),
        )
    )
    app.menu.add(rumps.MenuItem(
        "Edit config", callback=functools.partial(_open_in_editor, cfg.CONFIG_PATH)
    ))
    gh_title = f"Open GitHub ({app_behind} behind)" if app_behind else "Open GitHub"
    app.menu.add(rumps.MenuItem(gh_title, callback=_on_open_github))
    app.menu.add(rumps.MenuItem("Pull (update)", callback=_on_pull_update))
    app.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))


def _on_open_github(_: rumps.MenuItem) -> None:
    subprocess.run(["open", GITHUB_URL], capture_output=True)


def _on_toggle_pr(app: rumps.App, _: rumps.MenuItem) -> None:
    """Flip show_pr in state.json (so the choice persists), then refresh."""
    error = cfg.save_state(show_pr=not cfg.CONFIG.show_pr)
    if error:
        add_notice(app, f"Couldn't toggle PRs: {error}")
        return
    app._on_refresh(None)


def _on_pull_update(_: rumps.MenuItem) -> None:
    """Pull the latest repo-glance code; this serves as a software update.

    No restart or explicit refresh needed: the pull changes the code files'
    mtimes, which trips the fingerprint, so the next tick hot-reloads the
    new code. Only changes to main.py still require a manual restart.
    """
    result = subprocess.run(
        ["git", "-C", str(APP_REPO), "pull", "--ff-only"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        rumps.alert(
            "repo-glance update failed",
            result.stderr.strip() or result.stdout.strip(),
        )
