"""Repo rows: custom NSViews (hover highlight, click, right-click), their
attributed text, and the per-row actions submenu / context menu."""

from __future__ import annotations

import functools
import subprocess
import threading
import time
from pathlib import Path

import objc
import rumps
from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSEventModifierFlagControl,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSInsetRect,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSTimer,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
)

from . import config as cfg
from .cmux import AgentState, open_agent, open_in_cmux
from .codex import open_in_codex
from .fmt import AGENT_MARKS
from .notices import add_notice
from .prs import JIRA_BROWSE_URL
from .repos import switch_to_default_and_pull

_MONO_FONT = NSFont.monospacedSystemFontOfSize_weight_(13.0, 0.0)
# Same advance width as _MONO_FONT, so bold names don't break column alignment.
_MONO_BOLD_FONT = NSFont.monospacedSystemFontOfSize_weight_(13.0, 0.4)

ROW_HEIGHT = 22.0
ROW_INSET = 14.0  # horizontal text inset, roughly native menu metrics

IMPORTANT_COLOR = NSColor.systemOrangeColor


def _mono_attr(text: str, color: NSColor) -> NSAttributedString:
    return NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: _MONO_FONT, NSForegroundColorAttributeName: color}
    )


def _mono_item(text: str, callback=None, color: NSColor | None = None) -> rumps.MenuItem:
    item = rumps.MenuItem(text, callback=callback)
    attrs = {NSFontAttributeName: _MONO_FONT}
    if color is not None:
        attrs[NSForegroundColorAttributeName] = color
    attributed = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    item._menuitem.setAttributedTitle_(attributed)
    return item


def _on_open_url(url: str, _: rumps.MenuItem) -> None:
    subprocess.run(["open", url], capture_output=True)


def _open_in_app(app_name: str, repo: str) -> None:
    subprocess.run(["open", "-a", app_name, repo], capture_output=True)


# Actions offered in a repo row's submenu / right-click context menu.
CONTEXT_ACTIONS = (
    ("Open in cmux", open_in_cmux),
    ("Open in WebStorm", functools.partial(_open_in_app, "WebStorm")),
    ("Open in Cursor", functools.partial(_open_in_app, "Cursor")),
    ("Open in Codex", open_in_codex),
)


def _in_background(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _on_ctx_action(fn, repo: str, _item: rumps.MenuItem) -> None:
    fn(repo)


def _on_open_agent(repo: str, agent: AgentState, _item: rumps.MenuItem) -> None:
    _in_background(open_agent, repo, agent)


def _on_toggle_important(app, repo: str, _item: rumps.MenuItem) -> None:
    """Star/unstar a repo in state.json; the fingerprint picks it up within a tick."""
    name = Path(repo).name
    error = cfg.save_state(important=sorted(cfg.CONFIG.important ^ {name}))
    if error:
        add_notice(app, f"Couldn't change important: {error}")


def _on_merged_cleanup(app, repo: str, _item: rumps.MenuItem) -> None:
    """Switch a checkout whose PR merged back to its default branch and pull.
    Network-bound, so it runs off the main thread; failures become notices,
    success shows up as a branch change on the next tick."""
    def run() -> None:
        error = switch_to_default_and_pull(repo)
        if error:
            add_notice(app, f"{Path(repo).name}: {error}")
    _in_background(run)


def _action_items(
    app, repo: str, ticket: str | None, pr: dict | None, agent: AgentState | None
) -> list[rumps.MenuItem | None]:
    """Row actions; None entries become separators."""
    items: list[rumps.MenuItem | None] = []
    if agent and agent.state in AGENT_MARKS:
        title = "Jump to waiting Claude" if agent.state == "waiting" else "Jump to running agent"
        items.append(rumps.MenuItem(
            f"{AGENT_MARKS[agent.state][0]} {title}",
            callback=functools.partial(_on_open_agent, repo, agent),
        ))
    items += [
        rumps.MenuItem(title, callback=functools.partial(_on_ctx_action, fn, repo))
        for title, fn in CONTEXT_ACTIONS
    ]
    if pr:
        items.append(
            rumps.MenuItem(
                f"Open PR #{pr['number']}",
                callback=functools.partial(_on_open_url, pr["url"]),
            )
        )
    if ticket:
        items.append(
            rumps.MenuItem(
                f"Open {ticket}",
                callback=functools.partial(_on_open_url, JIRA_BROWSE_URL + ticket),
            )
        )
    if pr and pr.get("state") == "MERGED":
        items.append(None)
        items.append(rumps.MenuItem(
            "PR merged: switch to default branch & pull",
            callback=functools.partial(_on_merged_cleanup, app, repo),
        ))
    starred = Path(repo).name in cfg.CONFIG.important
    items.append(None)
    items.append(
        rumps.MenuItem(
            "Unmark important" if starred else "★ Mark important",
            callback=functools.partial(_on_toggle_important, app, repo),
        )
    )
    return items


def _add_items(menu: NSMenu, items: list) -> None:
    """Add rumps items to an NSMenu; None becomes a separator."""
    for item in items:
        menu.addItem_(NSMenuItem.separatorItem() if item is None else item._menuitem)


def _row_submenu(app, repo: str, ticket, pr, agent) -> tuple[NSMenu, list]:
    """The per-row actions submenu — hover the row to open it. A submenu runs
    inside the dropdown's own tracking session, which is the only way to show
    per-row actions while the dropdown stays open (see _popup_context_menu)."""
    sub = NSMenu.alloc().init()
    keep = _action_items(app, repo, ticket, pr, agent)
    _add_items(sub, keep)
    return sub, keep


def _popup_context_menu(view, event, repo: str) -> None:
    """Right/ctrl-click fallback. AppKit refuses to nest a second menu-tracking
    session inside the dropdown's (popUpContextMenu returns immediately without
    showing), so close the dropdown first, then pop the actions at the cursor.
    The zero-delay timer fires once the tracking runloop has unwound."""
    enclosing = view.enclosingMenuItem()
    outer = enclosing.menu() if enclosing is not None else None
    if outer is not None:
        outer.cancelTracking()

    def show(_timer) -> None:
        ctx = NSMenu.alloc().init()
        header = rumps.MenuItem(Path(repo).name)  # no callback -> disabled header
        keep = [header] + _action_items(
            getattr(view, "_rg_app", None), repo, getattr(view, "_rg_ticket", None),
            getattr(view, "_rg_pr", None), getattr(view, "_rg_agent", None),
        )
        _add_items(ctx, [header, None] + keep[1:])
        view._rg_ctx_keep = (ctx, keep)  # hold refs while the popup is up
        ctx.popUpMenuPositioningItem_atLocation_inView_(
            None, NSEvent.mouseLocation(), None
        )

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.0, False, show)


def _row_draw(view, _rect) -> None:
    hover = getattr(view, "_rg_hover", False)
    if hover:
        NSColor.selectedContentBackgroundColor().set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(view.bounds(), 5, 1), 4, 4
        ).fill()
    text = view._rg_text_hl if hover else view._rg_text
    text.drawAtPoint_((ROW_INSET, (ROW_HEIGHT - text.size().height) / 2))
    # Submenu arrow — native menus draw it, view-based items must themselves.
    arrow = _mono_attr(
        "▸", NSColor.whiteColor() if hover else NSColor.tertiaryLabelColor()
    )
    arrow.drawAtPoint_(
        (
            view.bounds().size.width - ROW_INSET,
            (ROW_HEIGHT - arrow.size().height) / 2,
        )
    )


def _row_event(view, kind: str, event) -> None:
    """All row-view mouse handling, kept out of the ObjC class so it stays
    hot-reloadable. `kind` is which NSView responder method fired."""
    if kind == "enter":
        view._rg_hover = True
        view.setNeedsDisplay_(True)
        return
    if kind == "exit":
        view._rg_hover = False
        view.setNeedsDisplay_(True)
        return
    now = time.monotonic()
    if kind in ("rdown", "rup"):
        # Both right handlers pop the menu; the timestamp keeps the up event
        # from re-popping right after the down event already did.
        if now - getattr(view, "_rg_ctx_at", 0.0) > 0.4:
            view._rg_ctx_at = now
            _popup_context_menu(view, event, view._rg_repo)
        return
    if kind == "up":
        if event.modifierFlags() & NSEventModifierFlagControl:
            view._rg_ctx_at = now
            _popup_context_menu(view, event, view._rg_repo)
            return
        item = view.enclosingMenuItem()
        if item is not None and item.menu() is not None:
            item.menu().cancelTracking()
        # A waiting/running agent jumps to its exact tab/workspace.
        _in_background(open_agent, view._rg_repo, getattr(view, "_rg_agent", None))


def _repo_row_view_class():
    """The custom NSView class for repo rows: draws its own hover highlight,
    opens cmux on left click, pops a context menu on right/ctrl click. Plain
    menu items can't do this — an NSMenuItem has a single action and never
    sees right clicks, so the row must handle its own mouse events.

    ObjC classes can't be re-registered, so the methods are thin delegates to
    this module's functions (which hot-reload). If the class body itself must
    change, or the delegates move to another module, bump the numeric suffix
    on the class name so a fresh class registers.
    """
    try:
        return objc.lookUpClass("RGRepoRowView3")
    except objc.error:
        pass

    class RGRepoRowView3(NSView):
        def updateTrackingAreas(self):
            objc.super(RGRepoRowView3, self).updateTrackingAreas()
            for area in list(self.trackingAreas()):
                self.removeTrackingArea_(area)
            self.addTrackingArea_(
                NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                    self.bounds(),
                    NSTrackingMouseEnteredAndExited
                    | NSTrackingActiveAlways
                    | NSTrackingInVisibleRect,
                    self,
                    None,
                )
            )

        def drawRect_(self, rect):
            _row_draw(self, rect)

        def mouseEntered_(self, event):
            _row_event(self, "enter", event)

        def mouseExited_(self, event):
            _row_event(self, "exit", event)

        def mouseDown_(self, event):
            _row_event(self, "down", event)

        def mouseUp_(self, event):
            _row_event(self, "up", event)

        def rightMouseDown_(self, event):
            _row_event(self, "rdown", event)

        def rightMouseUp_(self, event):
            _row_event(self, "rup", event)

    return RGRepoRowView3


def _on_row_select(repo: str, _item: rumps.MenuItem) -> None:
    open_in_cmux(repo)


def _row_text(
    summary: str, repo: str, agent: AgentState | None, highlighted: bool
) -> NSAttributedString:
    """The row's attributed text: the agent marker in its color, and for an
    important repo an orange star and a bold name. Highlighted rows stay all
    white (bold kept) so they read on the selection color."""
    text = NSMutableAttributedString.alloc().initWithAttributedString_(
        _mono_attr(summary, NSColor.whiteColor() if highlighted else NSColor.labelColor())
    )
    if agent and agent.state in AGENT_MARKS and not highlighted:
        text.addAttribute_value_range_(
            NSForegroundColorAttributeName, AGENT_MARKS[agent.state][1](), (0, 1)
        )
    name = Path(repo).name
    if name in cfg.CONFIG.important:
        # The gutter is only markers and spaces, so the first hits are ours.
        if not highlighted:
            text.addAttribute_value_range_(
                NSForegroundColorAttributeName, IMPORTANT_COLOR(), (summary.index("★"), 1)
            )
        text.addAttribute_value_range_(
            NSFontAttributeName, _MONO_BOLD_FONT, (summary.index(name), len(name))
        )
    return text


def _repo_row_item(
    app, repo: str, summary: str, width: float, ticket: str | None, pr: dict | None,
    agent: AgentState | None = None,
) -> rumps.MenuItem:
    view = _repo_row_view_class().alloc().initWithFrame_(
        NSMakeRect(0, 0, width, ROW_HEIGHT)
    )
    view._rg_app = app
    view._rg_repo = repo
    view._rg_ticket = ticket
    view._rg_pr = pr
    view._rg_agent = agent
    view._rg_text = _row_text(summary, repo, agent, highlighted=False)
    view._rg_text_hl = _row_text(summary, repo, agent, highlighted=True)
    # The callback keeps the item enabled under NSMenu's auto-enablement —
    # views inside a disabled item receive no mouse events. The view handles
    # clicks itself, so this only fires via keyboard selection.
    item = rumps.MenuItem(repo, callback=functools.partial(_on_row_select, repo))
    item._menuitem.setView_(view)
    sub, keep = _row_submenu(app, repo, ticket, pr, agent)
    item._menuitem.setSubmenu_(sub)
    view._rg_keep = (sub, keep)
    return item
