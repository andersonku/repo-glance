"""The refresh cycle: tick() on the main thread decides when to collect; a
worker thread builds a Snapshot (every git/gh/network call lives there); the
main thread renders it. cmux agent state has its own event-driven cycle.

All state lives on the app object (`_rg_*` attributes) because module
globals are re-initialised on every hot reload."""

from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import rumps
from Foundation import NSActivityUserInitiatedAllowingIdleSystemSleep, NSProcessInfo

from . import cmux
from . import config as cfg
from .config import TICK_SECONDS  # noqa: F401  (re-exported for the launcher)
from .fmt import _header, _name_width, repo_summary
from .menu import _build_menu, _set_status_icon, agent_states, install_menu_delegate
from .notices import add_notice
from .prs import _pr_map
from .repos import _changed_during, _fingerprint, discover_repos
from .snapshot import Snapshot
from .sync import _commits_behind, _sync_data
from .usage import _codex_usage, claude_usages
from .wip import _write_wip

# Agent state is recomputed when the `cmux events` stream reports a relevant
# event. Without the stream (cmux too old, not running) it's polled instead.
CMUX_POLL_SECONDS = 5  # no event stream: poll this often
CMUX_FALLBACK_SECONDS = 60  # with the stream: backstop for missed events
CMUX_WATCH_RETRY_SECONDS = 30  # restart a dead event stream at most this often


def tick(app: rumps.App, also_print: bool) -> None:
    """Called by the launcher every TICK_SECONDS on the main thread, after the
    code has been reloaded. Renders a finished snapshot if the worker left
    one, then starts a new collection when the fingerprint changed or
    refresh_seconds has elapsed since the last one."""
    started = time.perf_counter()
    try:
        _tick(app, also_print)
    finally:
        # (when, ms) for the slow-menu-open log.
        app._rg_tick = (time.monotonic(), (time.perf_counter() - started) * 1000)


def _keep_awake(app) -> None:
    """Opt out of App Nap. Left alone, macOS naps an idle menu-bar app:
    timers get coalesced and delayed by seconds and priority drops, so the
    first click after a quiet spell can take seconds to open the menu. The
    activity still lets the Mac sleep."""
    if getattr(app, "_rg_activity", None) is None:
        app._rg_activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep,
            "repo-glance responds to menu-bar clicks",
        )


def _tick(app: rumps.App, also_print: bool) -> None:
    if getattr(app, "_rg_pending", False):
        _render(app, also_print)
    _poll_cmux(app)
    worker = getattr(app, "_rg_worker", None)
    if worker is not None and worker.is_alive():
        return
    if _fingerprint() == getattr(app, "_rg_fingerprint", None) and (
        time.monotonic() - getattr(app, "_rg_last_refresh", 0.0)
        < cfg.CONFIG.refresh_seconds
    ):
        return
    refresh(app, also_print)


def _collect(app) -> Snapshot:
    """Gather the snapshot. Runs on the worker thread — this is where every
    git/gh/network call lives, so none of it can freeze the menu bar."""
    before = _fingerprint()
    infos = discover_repos()
    snap = Snapshot(
        infos=infos,
        prs=_pr_map(app, infos) if cfg.CONFIG.show_pr else {},
        usage=claude_usages(app) if cfg.CONFIG.show_usage else [],
        codex=_codex_usage() if cfg.CONFIG.show_usage else None,
        sync=_sync_data(app),
        app_behind=_commits_behind(app),
        fingerprint=(),
        collected_at=datetime.now(),
        changed_during=False,
    )
    # Fingerprint AFTER the git calls above: `git status` may itself rewrite
    # .git/index, which must not register as a new change on the next tick.
    snap.fingerprint = _fingerprint()
    snap.changed_during = _changed_during(before, snap.fingerprint)
    return snap


def _write_wip_safely(app, snap: Snapshot | None, agents: dict) -> None:
    try:
        _write_wip(snap, agents)
    except Exception as exc:
        print("repo-glance: writing wip_file failed:", file=sys.stderr)
        traceback.print_exc()
        add_notice(app, f"Writing {Path(cfg.CONFIG.wip_file).name} failed: {exc}")


def _worker(app: rumps.App) -> None:
    try:
        app._rg_snapshot = _collect(app)
        app._rg_pending = True
    except Exception as exc:
        print("repo-glance: collection failed:", file=sys.stderr)
        traceback.print_exc()
        add_notice(app, f"Refresh failed: {exc!r}")
        return
    _write_wip_safely(app, app._rg_snapshot, agent_states(app))


def refresh(app: rumps.App, also_print: bool) -> None:
    """Reload config, update the menu-bar icon, and start collecting a new
    snapshot in the background (a no-op if one is already being collected).

    Used at startup, by the Refresh-now menu item, and by tick() when a change
    is detected. The menu itself is rebuilt by tick() once the snapshot lands;
    until the first one does, a placeholder menu is shown.
    """
    cfg.CONFIG = cfg.load_config()
    _keep_awake(app)
    install_menu_delegate(app)
    if cfg.CONFIG.icon and Path(cfg.CONFIG.icon).is_file() and _set_status_icon(
        app, cfg.CONFIG.icon
    ):
        app.title = None
    else:
        app.icon = None
        app.title = cfg.CONFIG.title
    # Re-render right away so toggles (Hide PRs) and config errors show
    # instantly; the worker's snapshot replaces it within a couple of ticks.
    _build_menu(app, getattr(app, "_rg_snapshot", None))
    worker = getattr(app, "_rg_worker", None)
    if worker is not None and worker.is_alive():
        return
    # Reset the interval at start so a slow collection isn't immediately
    # followed by another.
    app._rg_last_refresh = time.monotonic()
    app._rg_worker = threading.Thread(target=_worker, args=(app,), daemon=True)
    app._rg_worker.start()


def _render(app: rumps.App, also_print: bool) -> None:
    """Main thread: rebuild the menu from the snapshot the worker left."""
    app._rg_pending = False  # before building, so a failing build can't loop
    snap = app._rg_snapshot
    _build_menu(app, snap)
    app._rg_fingerprint = snap.fingerprint
    if snap.changed_during:
        app._rg_last_refresh = 0.0  # next tick collects again
    if also_print:
        _print_cli_table(snap.infos, cfg.CONFIG.refresh_seconds, agent_states(app))


def _ensure_cmux_watcher(app) -> bool:
    """Keep one `cmux events` reader thread alive; it flags `_rg_cmux_dirty`
    on each relevant event. Returns whether the stream is (believed) up. A
    dead stream is restarted at most every CMUX_WATCH_RETRY_SECONDS."""
    watcher = getattr(app, "_rg_cmux_watcher", None)
    if watcher is not None and watcher.is_alive():
        return True
    now = time.monotonic()
    started = getattr(app, "_rg_cmux_watch_started", None)
    if started is not None and now - started < CMUX_WATCH_RETRY_SECONDS:
        return False
    app._rg_cmux_watch_started = now

    def mark_dirty() -> None:
        app._rg_cmux_dirty = True

    def run() -> None:
        try:
            cmux.watch_events(mark_dirty)
        except Exception:
            print("repo-glance: cmux event stream failed:", file=sys.stderr)
            traceback.print_exc()

    app._rg_cmux_watcher = threading.Thread(target=run, daemon=True)
    app._rg_cmux_watcher.start()
    return True


def _poll_cmux(app) -> None:
    """Main thread, every tick: start a background agent-state check when the
    event stream flagged a change, or as a timed backstop. When any repo's
    state changes, flag a re-render and rewrite WIP.md/WIP.json."""
    snap = getattr(app, "_rg_snapshot", None)
    if snap is None or not cmux.available():
        return
    streaming = _ensure_cmux_watcher(app)
    poller = getattr(app, "_rg_cmux_worker", None)
    if poller is not None and poller.is_alive():
        return
    now = time.monotonic()
    interval = CMUX_FALLBACK_SECONDS if streaming else CMUX_POLL_SECONDS
    if not getattr(app, "_rg_cmux_dirty", False) and (
        now - getattr(app, "_rg_cmux_polled", -interval) < interval
    ):
        return
    # Cleared before polling, so events arriving mid-poll trigger another.
    app._rg_cmux_dirty = False
    app._rg_cmux_polled = now
    repos = [i.repo for i in snap.infos]

    def run() -> None:
        try:
            states = cmux._cmux_agent_states(repos)
            if states == agent_states(app):
                return
            app._rg_agent_states = states
            app._rg_pending = True
            _write_wip_safely(app, app._rg_snapshot, states)
        except Exception:
            print("repo-glance: cmux poll failed:", file=sys.stderr)
            traceback.print_exc()

    app._rg_cmux_worker = threading.Thread(target=run, daemon=True)
    app._rg_cmux_worker.start()


def _print_cli_table(infos, interval: int, agents: dict | None = None) -> None:
    name_w = _name_width(infos)
    print("\033[2J\033[H", end="")
    for _path, message in cfg.CONFIG.errors:
        print(f"⚠ {message}")
    print(_header(name_w, agents))
    print("-" * len(_header(name_w, agents)))
    for info in infos:
        print(repo_summary(info, name_w, agents))
    print()
    print(
        f"Last refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
        f"(refresh on change, full refresh every {interval}s — Ctrl-C to exit)"
    )
    sys.stdout.flush()
