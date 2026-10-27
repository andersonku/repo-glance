"""Dismissible one-line messages shown at the top of the menu: failed menu
actions and failed collections. They live on the app object (module globals
are wiped by reload) and can be added from any thread."""

from __future__ import annotations

MAX_NOTICES = 5


def notices(app) -> list[str]:
    return list(getattr(app, "_rg_notices", []))


def add_notice(app, text: str) -> None:
    """Show `text` until dismissed; repeats of a visible notice are dropped."""
    current = notices(app)
    if text in current:
        return
    app._rg_notices = (current + [text])[-MAX_NOTICES:]
    _rerender(app)


def dismiss_notice(app, text: str) -> None:
    app._rg_notices = [n for n in notices(app) if n != text]
    _rerender(app)


def _rerender(app) -> None:
    # tick() re-renders the current snapshot when this is set.
    if getattr(app, "_rg_snapshot", None) is not None:
        app._rg_pending = True
