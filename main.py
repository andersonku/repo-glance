"""Thin launcher for repo-glance.

Owns only what cannot be recreated at runtime: the Cocoa event loop, the
menu-bar item, and the tick timer. Everything else lives in logic.py,
which is reloaded on every tick — so code edits take effect within seconds
without restarting the app. Only changes to THIS file need a restart.

Never `from logic import ...` here: that would pin references to old code.
Always go through the `logic` module attribute so reloads take effect.
"""

from __future__ import annotations

import importlib
import sys
import time
import traceback

import rumps

import logic


def _reload_logic() -> None:
    """Reload logic.py; on failure (e.g. syntax error) keep the old version."""
    global logic
    try:
        logic = importlib.reload(logic)
    except Exception:
        print("repo-glance: reload of logic.py failed:", file=sys.stderr)
        traceback.print_exc()


class PlaymakerStatusApp(rumps.App):
    def __init__(self, also_print: bool = True) -> None:
        super().__init__("RG", quit_button=None)
        self._also_print = also_print
        self._timer = rumps.Timer(self._on_tick, logic.TICK_SECONDS)
        self._refresh()
        self._timer.start()

    def _refresh(self) -> None:
        """Unconditional full refresh (startup and the Refresh-now menu item)."""
        _reload_logic()
        try:
            logic.refresh(self, also_print=self._also_print)
        except Exception:
            print("repo-glance: refresh failed:", file=sys.stderr)
            traceback.print_exc()

    def _on_tick(self, _: rumps.Timer) -> None:
        _reload_logic()
        try:
            logic.tick(self, also_print=self._also_print)
        except Exception:
            print("repo-glance: tick failed:", file=sys.stderr)
            traceback.print_exc()

    def _on_refresh(self, _: rumps.MenuItem) -> None:
        self._refresh()


def cli_loop() -> None:
    state: dict = {}
    try:
        while True:
            _reload_logic()
            try:
                interval = logic.cli_tick(state)
            except Exception:
                print("repo-glance: refresh failed:", file=sys.stderr)
                traceback.print_exc()
                interval = 60
            time.sleep(interval)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        cli_loop()
    else:
        PlaymakerStatusApp(also_print="--no-cli" not in sys.argv).run()
