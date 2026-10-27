"""--cli mode: the same snapshot, agent markers, and WIP.md/WIP.json as the
menu-bar app, printed as a table instead of a menu."""

from __future__ import annotations

import time
from types import SimpleNamespace

from . import config as cfg
from .app import _collect, _poll_cmux, _print_cli_table, _write_wip_safely
from .config import TICK_SECONDS
from .menu import agent_states
from .repos import _fingerprint


def cli_tick(state: dict) -> int:
    """Collect and print when something changed, refresh_seconds elapsed, or
    cmux agent state changed; return the seconds to sleep. `state` is owned
    by the launcher so it survives reloads; its "app" entry stands in for
    the menu-bar app object that the shared helpers keep their caches on."""
    cfg.CONFIG = cfg.load_config()
    app = state.setdefault("app", SimpleNamespace())
    _poll_cmux(app)  # flags _rg_pending (and rewrites WIP) on agent changes
    due = _fingerprint() != getattr(app, "_rg_fingerprint", None) or (
        time.monotonic() - getattr(app, "_rg_last_refresh", 0.0)
        >= cfg.CONFIG.refresh_seconds
    )
    if due:
        app._rg_snapshot = _collect(app)
        app._rg_fingerprint = app._rg_snapshot.fingerprint
        app._rg_last_refresh = time.monotonic()
        _write_wip_safely(app, app._rg_snapshot, agent_states(app))
    if due or getattr(app, "_rg_pending", False):
        app._rg_pending = False
        _print_cli_table(app._rg_snapshot.infos, cfg.CONFIG.refresh_seconds, agent_states(app))
    return TICK_SECONDS
