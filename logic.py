"""All app logic for repo-glance. Reloaded by main.py on every tick,
so edits to this file take effect without restarting the app."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
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
    NSImage,
    NSInsetRect,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTimer,
    NSTrackingMouseEnteredAndExited,
    NSView,
)

CONFIG_PATH = Path.home() / ".config" / "repo-glance" / "config.toml"

# How often the launcher ticks. Each tick is a cheap stat-based change check;
# a full refresh runs only on change or every refresh_seconds.
TICK_SECONDS = 2

DEFAULTS = {
    "scan_dirs": ["~/dev", "~/dev2"],
    "repo_pattern": r"playmaker\d*",
    "hide_pattern": [],
    "refresh_seconds": 60,
    "title": "RG",
    "sort": "name",
    "active_days": 30,
    "branch_width": 25,
    "show_pr": True,
    "show_usage": True,
    "icon": "",
    "wip_file": "~/.config/repo-glance/WIP.md",
    "important": [],
}

# Comment block above each key — used for the sample config and for the
# blocks appended to existing config files when an update adds new keys.
KEY_COMMENTS = {
    "scan_dirs": '# Folders to scan for repo checkouts. "~" and $ENV_VARS are expanded.',
    "repo_pattern": (
        "# A directory is shown if its name FULLY matches any of these regexes.\n"
        '# A list also works: repo_pattern = ["playmaker\\\\d*", "fastbreak\\\\d*"]'
    ),
    "hide_pattern": (
        "# Hide repos whose directory name FULLY matches any of these regexes, even if\n"
        '# repo_pattern matches them. A string or a list, e.g. ["playmaker[5-7]"].'
    ),
    "refresh_seconds": (
        "# Seconds between unconditional full refreshes. Git changes (commits, branch\n"
        "# switches, staging) and config edits are detected within ~2s regardless; this\n"
        "# interval is the backstop that also catches unstaged working-tree edits."
    ),
    "title": "# Menu-bar title.",
    "sort": (
        '# Order repos are listed in: "name" (default, grouped by scan dir),\n'
        '# "oldest" (oldest last commit first), or "newest" (most recent first).'
    ),
    "active_days": (
        "# Hide repos whose last commit is older than this many days. 0 shows all."
    ),
    "branch_width": (
        '# Branch column width; longer branch names are truncated with "...".'
    ),
    "show_pr": (
        "# Show an indented, clickable PR line under each repo whose branch has a\n"
        "# pull request (resolved via the gh CLI)."
    ),
    "show_usage": (
        "# Show a first row with your Claude 5-hour session usage and when it\n"
        "# resets. Needs a Claude Code login; the token is read from the keychain,\n"
        "# so macOS asks for permission the first time. Also shows a Codex usage\n"
        "# row when ~/.codex session files are present (read locally, no network)."
    ),
    "icon": (
        "# Path to a .png/.svg file shown as the menu-bar icon instead of the title\n"
        '# text. "~" and $ENV_VARS are expanded; empty shows the title text.'
    ),
    "wip_file": (
        "# Markdown file rewritten on every refresh with the same info as the menu\n"
        "# (repos, branches, PRs, usage), for other agents to read. Empty disables."
    ),
    "important": (
        "# Repo directory names shown with a star. Toggled per repo by Mark/Unmark\n"
        "# important in its context menu, which rewrites this key."
    ),
}


def _toml_value(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(value)  # JSON strings/ints are valid TOML


def _key_block(key: str) -> str:
    return f"{KEY_COMMENTS[key]}\n{key} = {_toml_value(DEFAULTS[key])}\n"


SAMPLE_CONFIG = (
    "# repo-glance configuration.\n"
    "# Changes are picked up within a couple of seconds — no restart needed.\n\n"
    + "\n".join(_key_block(k) for k in DEFAULTS)
)


@dataclass(frozen=True)
class Config:
    scan_dirs: list[Path]
    repo_pattern: re.Pattern
    hide_pattern: re.Pattern | None
    refresh_seconds: int
    title: str
    sort: str
    active_days: int
    branch_width: int
    show_pr: bool
    show_usage: bool
    icon: str
    wip_file: str
    important: frozenset[str]


def _write_sample_config() -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(SAMPLE_CONFIG)
    except OSError:
        pass


def _append_missing_keys(parsed: dict) -> None:
    """Append commented default blocks for keys the user's config file doesn't
    have yet, so options added by software updates become visible."""
    missing = [k for k in DEFAULTS if k not in parsed]
    if not missing:
        return
    try:
        with CONFIG_PATH.open("a") as f:
            f.write("\n" + "\n".join(_key_block(k) for k in missing))
    except OSError:
        pass


def _full_match_pattern(patterns) -> re.Pattern | None:
    """Combine a regex string or list into one anchored pattern; None if empty."""
    if isinstance(patterns, str):
        patterns = [patterns]
    patterns = [p for p in patterns if p]
    if not patterns:
        return None
    combined = "|".join(f"(?:{p})" for p in patterns)
    return re.compile(f"^(?:{combined})$")


def load_config() -> Config:
    """Load config from CONFIG_PATH, falling back to defaults for missing keys."""
    raw = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("rb") as f:
                parsed = tomllib.load(f)
            raw.update(parsed)
            _append_missing_keys(parsed)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"repo-glance: ignoring bad config: {exc}", file=sys.stderr)
    else:
        _write_sample_config()
    scan_dirs = [
        Path(os.path.expandvars(os.path.expanduser(d))) for d in raw["scan_dirs"]
    ]
    return Config(
        scan_dirs=scan_dirs,
        repo_pattern=_full_match_pattern(raw["repo_pattern"]),
        hide_pattern=_full_match_pattern(raw["hide_pattern"]),
        refresh_seconds=int(raw["refresh_seconds"]),
        title=str(raw["title"]),
        sort=str(raw["sort"]),
        active_days=int(raw["active_days"]),
        branch_width=int(raw["branch_width"]),
        show_pr=bool(raw["show_pr"]),
        show_usage=bool(raw["show_usage"]),
        icon=os.path.expandvars(os.path.expanduser(str(raw["icon"]))),
        wip_file=os.path.expandvars(os.path.expanduser(str(raw["wip_file"]))),
        important=frozenset(str(n) for n in raw["important"]),
    )


CONFIG = load_config()


@dataclass
class RepoInfo:
    repo: str
    branch: str
    epoch: int
    last_iso: str
    uncommitted: int
    is_git: bool


def _shown(name: str) -> bool:
    if not CONFIG.repo_pattern or not CONFIG.repo_pattern.match(name):
        return False
    return not (CONFIG.hide_pattern and CONFIG.hide_pattern.match(name))


def _natural_key(name: str) -> list:
    """Sort key that orders digit runs numerically: playmaker9 < playmaker10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _candidate_repos() -> list[str]:
    """Scan configured dirs for repos matching the pattern. Stat-only, no git calls."""
    repos: list[str] = []
    for root in CONFIG.scan_dirs:
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
    if CONFIG.active_days > 0:
        cutoff = time.time() - CONFIG.active_days * 86400
        infos = [i for i in infos if i.epoch >= cutoff]
    if CONFIG.sort == "newest":
        infos.sort(key=lambda i: i.epoch, reverse=True)
    elif CONFIG.sort == "oldest":
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


def _changed_during(before: tuple, after: tuple) -> bool:
    """Did anything other than .git/index change between two fingerprints?
    Used to spot a commit/checkout that landed while a collection was running.
    Index changes are ignored because the collection's own `git status` may
    rewrite it; a staging-only change is caught by the periodic refresh."""
    if len(before) != len(after):
        return True
    stride = len(_FINGERPRINT_GIT_FILES) + 1  # + the repo path entry
    index_pos = _FINGERPRINT_GIT_FILES.index("index") + 1
    return any(
        a != b and not (i >= 2 and (i - 2) % stride == index_pos)
        for i, (a, b) in enumerate(zip(before, after))
    )


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
            # new-workspace --focus selects the new workspace; the bare-path
            # form doesn't. It needs cmux running, so fall back to the bare
            # form (which launches cmux) if it fails.
            result = _cmux("new-workspace", "--cwd", repo, "--focus", "true")
            if result.returncode != 0:
                _cmux(repo)
    except (subprocess.SubprocessError, OSError):
        return
    subprocess.run(["open", "-b", CMUX_BUNDLE_ID], capture_output=True)


# Agent state changes without touching git, so it's polled on its own, faster
# cadence instead of riding on the full refresh.
CMUX_POLL_SECONDS = 5

CMUX_WAITING_BODY = "Claude is waiting for your input"

# Marker shown before the repo name for each agent state, and its color.
AGENT_MARKS = {
    "running": ("◐", NSColor.systemGreenColor),
    "waiting": ("✳", NSColor.systemRedColor),
}


def _iso(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _cmux_agent_states(repos: list[str]) -> dict[str, str]:
    """repo -> "running" or "waiting", from its cmux workspaces (cwd at or
    under the repo). Running wins: any workspace whose agent is working
    (cmux's any_agent_running signal). Otherwise waiting: the newest "Claude
    is waiting for your input" notification is newer than the last prompt
    submitted there, i.e. Claude asked and you haven't replied. cmux keeps
    notifications after they're read, so read state doesn't count.
    Off the main thread only: two list calls, then one status call per
    matching workspace, in parallel."""
    try:
        workspaces = json.loads(_cmux("rpc", "workspace.list").stdout).get("workspaces", [])
        notes = json.loads(_cmux("--json", "list-notifications").stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, AttributeError):
        return {}
    waiting_since: dict[str, datetime] = {}
    for note in notes if isinstance(notes, list) else []:
        at = _iso(note.get("created_at"))
        ws_id = note.get("workspace_id")
        if at and ws_id and CMUX_WAITING_BODY in (note.get("body") or ""):
            waiting_since[ws_id] = max(at, waiting_since.get(ws_id, at))

    def owner(cwd: str) -> str | None:
        for repo in repos:
            if cwd == repo or cwd.startswith(repo + "/"):
                return repo
        return None

    owned = [
        (ws, repo)
        for ws in workspaces
        if ws.get("id") and (repo := owner(ws.get("current_directory") or ""))
    ]

    def running(ws_id: str) -> bool:
        try:
            out = _cmux("--json", "workspace", "status", "--workspace", ws_id).stdout
            return bool(json.loads(out).get("signals", {}).get("any_agent_running"))
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            return False

    with ThreadPoolExecutor(max_workers=8) as ex:
        flags = list(ex.map(lambda t: running(t[0]["id"]), owned))
    states: dict[str, str] = {}
    for (ws, repo), is_running in zip(owned, flags):
        if is_running:
            states[repo] = "running"
            continue
        asked = waiting_since.get(ws["id"])
        replied = _iso(ws.get("latest_submitted_at"))
        if asked and (replied is None or asked > replied):
            states.setdefault(repo, "waiting")
    return states


def _poll_cmux(app: rumps.App) -> None:
    """Main thread, every tick: start a background agent-state check at most
    every CMUX_POLL_SECONDS. When any repo's state changes, flag a re-render
    and rewrite WIP.md."""
    snap = getattr(app, "_rg_snapshot", None)
    if snap is None or not Path(CMUX_BIN).exists():
        return
    poller = getattr(app, "_rg_cmux_worker", None)
    if poller is not None and poller.is_alive():
        return
    now = time.monotonic()
    if now - getattr(app, "_rg_cmux_polled", 0.0) < CMUX_POLL_SECONDS:
        return
    app._rg_cmux_polled = now
    repos = [i.repo for i in snap.infos]

    def run() -> None:
        try:
            agents = _cmux_agent_states(repos)
            if agents == getattr(app, "_rg_agents", {}):
                return
            app._rg_agents = agents
            app._rg_pending = True
            _write_wip(app._rg_snapshot, agents)
        except Exception:
            print("repo-glance: cmux poll failed:", file=sys.stderr)
            traceback.print_exc()

    app._rg_cmux_worker = threading.Thread(target=run, daemon=True)
    app._rg_cmux_worker.start()


LAST_W = 7
COUNT_W = 3

_MONO_FONT = NSFont.monospacedSystemFontOfSize_weight_(13.0, 0.0)
# Same advance width as _MONO_FONT, so bold names don't break column alignment.
_MONO_BOLD_FONT = NSFont.monospacedSystemFontOfSize_weight_(13.0, 0.4)


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
    return f"{days}d"


def _name_width(infos: list[RepoInfo]) -> int:
    return max((len(Path(i.repo).name) for i in infos), default=12)


def _gutter(repo: str, agents: dict[str, str] | None = None) -> str:
    """Marker columns before the name: the agent state (AGENT_MARKS) from
    cmux, and "★" when it's marked important. Each column only appears while
    at least one repo uses it."""
    out = ""
    if agents:
        state = agents.get(repo)
        out += f"{AGENT_MARKS[state][0]} " if state else "  "
    if CONFIG.important:
        out += "★ " if Path(repo).name in CONFIG.important else "  "
    return out


def repo_summary(
    info: RepoInfo, name_w: int = 12, agents: dict[str, str] | None = None
) -> str:
    name = Path(info.repo).name
    star = _gutter(info.repo, agents)
    if not info.is_git:
        return f"{star}{name:<{name_w}}  (not a git repo)"
    branch_w = CONFIG.branch_width
    branch = info.branch
    if len(branch) > branch_w:
        branch = branch[: branch_w - 3] + "..."
    last = _relative_days(info.last_iso) if info.last_iso else "-"
    dirty = f"~{info.uncommitted}" if info.uncommitted else ""
    return (
        f"{star}{name:<{name_w}}  "
        f"{branch:<{branch_w}}  "
        f"{last:<{LAST_W}}  "
        f"{dirty}"
    )


def _mono_item(text: str, callback=None) -> rumps.MenuItem:
    item = rumps.MenuItem(text, callback=callback)
    attributed = NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: _MONO_FONT}
    )
    item._menuitem.setAttributedTitle_(attributed)
    return item


def tick(app: rumps.App, also_print: bool) -> None:
    """Called by the launcher every TICK_SECONDS on the main thread, after this
    module has been reloaded. Renders a finished snapshot if the worker left
    one, then starts a new collection when the fingerprint changed or
    refresh_seconds has elapsed since the last one."""
    if getattr(app, "_rg_pending", False):
        _render(app, also_print)
    _poll_cmux(app)
    worker = getattr(app, "_rg_worker", None)
    if worker is not None and worker.is_alive():
        return
    if _fingerprint() == getattr(app, "_rg_fingerprint", None) and (
        time.monotonic() - getattr(app, "_rg_last_refresh", 0.0)
        < CONFIG.refresh_seconds
    ):
        return
    refresh(app, also_print)


ICON_HEIGHT = 18  # standard menu-bar icon height in points


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


@dataclass
class Snapshot:
    """Everything the menu is rendered from, gathered off the main thread."""
    infos: list[RepoInfo]
    prs: dict[str, dict | None]
    usage: tuple | None
    codex: tuple | None
    sync: tuple | None
    app_behind: int
    fingerprint: tuple
    collected_at: datetime
    changed_during: bool  # a commit/checkout landed mid-collection; recollect


def _collect(app: rumps.App) -> Snapshot:
    """Gather the snapshot. Runs on the worker thread — this is where every
    git/gh/network call lives, so none of it can freeze the menu bar."""
    before = _fingerprint()
    infos = discover_repos()
    snap = Snapshot(
        infos=infos,
        prs=_pr_map(app, infos) if CONFIG.show_pr else {},
        usage=_usage(app) if CONFIG.show_usage else None,
        codex=_codex_usage() if CONFIG.show_usage else None,
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


def _worker(app: rumps.App) -> None:
    try:
        app._rg_snapshot = _collect(app)
        app._rg_pending = True
    except Exception:
        print("repo-glance: collection failed:", file=sys.stderr)
        traceback.print_exc()
        return
    try:
        _write_wip(app._rg_snapshot, getattr(app, "_rg_agents", {}))
    except Exception:
        print("repo-glance: writing wip_file failed:", file=sys.stderr)
        traceback.print_exc()


def refresh(app: rumps.App, also_print: bool) -> None:
    """Reload config, update the menu-bar icon, and start collecting a new
    snapshot in the background (a no-op if one is already being collected).

    Used at startup, by the Refresh-now menu item, and by tick() when a change
    is detected. The menu itself is rebuilt by tick() once the snapshot lands;
    until the first one does, a placeholder menu is shown.

    All state lives on the app object because module globals are wiped by
    reload.
    """
    global CONFIG
    CONFIG = load_config()
    if CONFIG.icon and Path(CONFIG.icon).is_file() and _set_status_icon(
        app, CONFIG.icon
    ):
        app.title = None
    else:
        app.icon = None
        app.title = CONFIG.title
    # Re-render right away so config-driven toggles (Hide PRs) feel instant;
    # the worker's snapshot replaces it within a couple of ticks.
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
        _print_cli_table(snap.infos, CONFIG.refresh_seconds)


# gh lives in /opt/homebrew/bin, which isn't on PATH when launched as an app.
GH_BIN = shutil.which("gh") or "/opt/homebrew/bin/gh"

PR_TITLE_W = 50  # PR titles longer than this are truncated with "..."


def _pr_lookup(repo: str, selector: str | None = None) -> dict | None:
    """PR for the repo's current branch via `gh pr view` (resolves the branch
    itself), or for an explicit number/branch/URL `selector`. None if there is
    no PR, gh is missing, or gh isn't authed."""
    try:
        result = subprocess.run(
            [GH_BIN, "pr", "view", *([selector] if selector else []), "--json",
             "number,url,title,state,isDraft,reviewDecision,statusCheckRollup"],
            cwd=repo, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None


_CI_FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE"}
_CI_DONE = _CI_FAILED | {"SUCCESS", "NEUTRAL", "SKIPPED"}


def _ci_state(pr: dict) -> str | None:
    """"failed" / "running" / "passed", or None when the PR has no checks."""
    # Rollup entries are CheckRuns (conclusion, empty until completed) or
    # commit StatusContexts (state).
    results = {
        c.get("conclusion") or c.get("state") or "PENDING"
        for c in pr.get("statusCheckRollup") or []
    }
    if not results:
        return None
    if results & _CI_FAILED:
        return "failed"
    if results - _CI_DONE:
        return "running"
    return "passed"


def _pr_extra(pr: dict) -> str:
    """Noteworthy PR state as short labels ("CI failed, Approved"). Quiet
    states (open, CI green, no review yet) produce nothing."""
    # Merged PRs report no usable CI status, and review state no longer
    # matters — "Merged" says it all.
    if pr.get("state") == "MERGED":
        return "Merged"
    labels = []
    if pr.get("state") == "CLOSED":
        labels.append("Closed")
    if pr.get("isDraft"):
        labels.append("Draft")
    ci = _ci_state(pr)
    if ci in ("failed", "running"):
        labels.append(f"CI {ci}")
    decision = pr.get("reviewDecision")
    if decision == "APPROVED":
        labels.append("Approved")
    elif decision == "CHANGES_REQUESTED":
        labels.append("Changes requested")
    return ", ".join(labels)


def _pr_map(app: rumps.App, infos: list[RepoInfo]) -> dict[str, dict | None]:
    """repo -> PR info for the branch it is on. Lookups hit the network, so
    results are cached by (repo, branch) on the app object (survives reloads)
    and re-checked at most every FETCH_SECONDS — which also picks up PRs
    opened after a branch was first seen."""
    cache = getattr(app, "_rg_pr_cache", None)
    if cache is None:
        cache = app._rg_pr_cache = {}
    now = time.monotonic()
    targets = [i for i in infos if i.is_git and i.branch]
    stale = [
        i for i in targets
        if (i.repo, i.branch) not in cache
        or now - cache[(i.repo, i.branch)][0] >= FETCH_SECONDS
    ]
    if stale:
        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(lambda i: _pr_lookup(i.repo), stale))
        for info, pr in zip(stale, results):
            cache[(info.repo, info.branch)] = (now, pr)
    return {i.repo: cache[(i.repo, i.branch)][1] for i in targets}


# TEMPORARY, hard-coded: track the Node 24 upgrade (PR #10405) against main —
# both the remote PR branch and the local playmaker9 checkout of it. Remove
# once that project lands.
SYNC_REPO = str(Path.home() / "dev2" / "playmaker9")
SYNC_BRANCH = "feat/GO-0-node-24"
SYNC_PR_URL = "https://github.com/GameOnSports/playmaker/pull/10405"


def _fetch_refs(repo: str, *refs: str) -> None:
    """Refresh origin/<ref> for the given refs only — much cheaper than a full
    fetch on a big repo. Never prompts for credentials (would hang the refresh)."""
    try:
        subprocess.run(
            ["git", "-C", repo, "fetch", "--quiet", "origin", *refs],
            capture_output=True,
            timeout=20,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        pass


def _behind_ahead(repo: str, base: str, head: str) -> tuple[int, int]:
    """(commits on base missing from head, commits on head missing from base)."""
    out = _git(repo, "rev-list", "--left-right", "--count", f"{base}...{head}")
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return 0, 0
    return int(parts[0]), int(parts[1])


def _sync_data(app: rumps.App) -> tuple[str | None, tuple[int, int], tuple[int, int]] | None:
    """(CI state, remote (behind, ahead), local (behind, ahead)) vs origin/main.
    Worker thread. The fetch and PR lookup are rate-limited (timestamp on the
    app object survives reloads); the rev-lists are local and run every
    refresh, so the counts self-correct right after a rebase/push."""
    if not Path(SYNC_REPO, ".git").exists():
        return None
    now = time.monotonic()
    last = getattr(app, "_rg_sync_fetched", None)
    if last is None or now - last >= FETCH_SECONDS:
        _fetch_refs(SYNC_REPO, "main", SYNC_BRANCH)
        # Keep the last good PR reading if gh fails this round.
        app._rg_sync_pr = _pr_lookup(SYNC_REPO, SYNC_PR_URL) or getattr(
            app, "_rg_sync_pr", None
        )
        app._rg_sync_fetched = now
    pr = getattr(app, "_rg_sync_pr", None)
    return (
        _ci_state(pr) if pr else None,
        _behind_ahead(SYNC_REPO, "origin/main", f"origin/{SYNC_BRANCH}"),
        _behind_ahead(SYNC_REPO, "origin/main", "HEAD"),
    )


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


def _sync_lines(data) -> tuple:
    """(label, (behind, ahead), extra) for the remote PR branch and the local
    checkout."""
    ci, remote, local = data
    return (
        ("PR #10405 (remote)", remote, f"CI {ci}" if ci else ""),
        ("playmaker9 (local)", local, ""),
    )


def _on_open_url(url: str, _: rumps.MenuItem) -> None:
    subprocess.run(["open", url], capture_output=True)


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_SETTINGS_URL = "https://claude.ai/settings/usage"
# The usage endpoint allows roughly one call every few minutes; a second call
# 20s after a successful one already 429s. Its 429 carries "Retry-After: 0" and
# no reset timestamp, so there is nothing useful to back off against — hence a
# fixed interval, and a failed poll just leaves the last reading up as stale.
USAGE_SECONDS = 300
USAGE_BAR_W = 10
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _oauth_token() -> str | None:
    """Claude Code's OAuth access token, read from the login keychain. None if
    it's missing or already expired — Claude Code itself owns refreshing it, so
    a stale token just means no usage row until it next runs."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        oauth = json.loads(result.stdout)["claudeAiOauth"]
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, KeyError):
        return None
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 <= time.time():
        return None
    return oauth.get("accessToken")


def _usage_lookup() -> tuple[int, datetime] | None:
    """(percent used, local reset time) for the current 5-hour session limit —
    the same numbers Claude Code's own /usage shows. None on any failure."""
    token = _oauth_token()
    if not token:
        return None
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None
    # The "limits" list is the current shape; "five_hour" is the older
    # equivalent, kept as a fallback in case one of them goes away.
    session = next(
        (x for x in data.get("limits") or [] if x.get("kind") == "session"), None
    ) or data.get("five_hour") or {}
    percent = session.get("percent", session.get("utilization"))
    resets = session.get("resets_at")
    if percent is None or not resets:
        return None
    try:
        when = datetime.fromisoformat(resets).astimezone()
    except ValueError:
        return None
    return round(percent), when


def _usage(app: rumps.App) -> tuple[int, datetime, datetime, bool] | None:
    """(percent, reset time, when it was read, stale) for the 5-hour session limit.

    The endpoint is tightly rate-limited, so it's polled at most once per
    USAGE_SECONDS and a failed poll (429, expired token, no network) keeps
    returning the last good reading, flagged stale, rather than blanking the
    row — the same fallback Claude Code's own /usage uses. `stale` tracks the
    last poll's outcome rather than the reading's age, so a 429 is reflected
    immediately instead of after the reading happens to get old enough. State
    lives on the app object so it survives module reloads.
    """
    state = getattr(app, "_rg_usage", (None, None, True))
    if len(state) != 3 or (state[1] is not None and len(state[1]) != 3):
        state = (None, None, True)  # state from an older build; re-poll
    attempted, reading, polled_ok = state
    now = time.monotonic()
    if attempted is None or now - attempted >= USAGE_SECONDS:
        fresh = _usage_lookup()
        polled_ok = fresh is not None
        if fresh:
            reading = (*fresh, datetime.now().astimezone())
        app._rg_usage = (now, reading, polled_ok)
    if not reading:
        return None
    # Once the window it described has rolled over, a stale percent is not
    # merely old, it's wrong — drop it rather than show a number from the
    # previous session.
    if reading[1] <= datetime.now().astimezone():
        app._rg_usage = (app._rg_usage[0], None, polled_ok)
        return None
    return (*reading, not polled_ok)


def usage_summary(
    name: str, percent: int, resets: datetime | None, read_at: datetime, stale: bool
) -> str:
    """One compact segment of the shared usage row."""
    filled = min(USAGE_BAR_W, max(0, round(percent / 100 * USAGE_BAR_W)))
    bar = "█" * filled + "░" * (USAGE_BAR_W - filled)
    now = datetime.now().astimezone()
    seg = f"{name} {bar} {percent:>3}%"
    if resets is not None:
        # Same-day resets show a time; longer windows (Codex is weekly) a date.
        fmt = "%H:%M" if (resets - now).total_seconds() < 86400 else "%d/%m"
        seg += f" resets {resets.strftime(fmt)}"
    if stale:
        fmt = "%H:%M" if read_at.date() == now.date() else "%d/%m %H:%M"
        seg += f" (as of {read_at.strftime(fmt)})"
    return seg


CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_USAGE_URL = "https://chatgpt.com/codex/settings/usage"
# A reading older than this is flagged "(as of ...)" — Codex only writes one
# when it's actually used, so old readings are common.
CODEX_STALE_SECONDS = 3600


def _codex_rate_limits_in(path: Path) -> tuple[int, datetime, datetime] | None:
    """(percent used, reset time, when recorded) from the last rate_limits
    event in one session rollout file. Only the file's tail is read — rollouts
    grow to megabytes and the newest event is what matters."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            entry = json.loads(line)
            primary = entry["payload"]["rate_limits"]["primary"]
            percent = round(primary["used_percent"])
            resets = datetime.fromtimestamp(primary["resets_at"]).astimezone()
            read_at = datetime.fromisoformat(
                entry["timestamp"].replace("Z", "+00:00")
            ).astimezone()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            continue
        return percent, resets, read_at
    return None


def _codex_usage() -> tuple[int, datetime | None, datetime, bool] | None:
    """Codex usage for the primary rate-limit window, read from the local
    session rollouts Codex writes on every turn (~/.codex/sessions/Y/M/D/).
    None when Codex isn't installed or no reading can be found."""
    try:
        day_dirs = sorted(CODEX_SESSIONS_DIR.glob("*/*/*"), reverse=True)[:3]
    except OSError:
        return None
    # Filenames embed their timestamp, so name order is time order.
    files = sorted((f for d in day_dirs for f in d.glob("rollout-*.jsonl")), reverse=True)
    for path in files[:5]:
        reading = _codex_rate_limits_in(path)
        if reading:
            break
    else:
        return None
    percent, resets, read_at = reading
    now = datetime.now().astimezone()
    if resets <= now:
        # The window rolled over with no Codex activity since: current usage
        # is zero and the next window's reset time isn't knowable yet.
        return 0, None, read_at, False
    return percent, resets, read_at, (now - read_at).total_seconds() > CODEX_STALE_SECONDS


def _build_menu(app: rumps.App, snap: Snapshot | None) -> None:
    """Main thread. Pure rendering of a snapshot — no git, gh, or network here.
    With no snapshot yet (startup), shows a placeholder with the footer."""
    app.menu.clear()
    if snap is None:
        app.menu.add(_mono_item("Loading…"))
        app.menu.add(rumps.separator)
        _add_footer(app, refreshed=None, app_behind=0)
        return
    if CONFIG.show_usage:
        segments = [
            usage_summary("Claude", *snap.usage) if snap.usage
            else "Claude usage unavailable"
        ]
        if snap.codex:
            segments.append(usage_summary("Codex", *snap.codex))
        app.menu.add(
            _mono_item(
                "  │  ".join(segments),
                callback=functools.partial(_on_open_url, USAGE_SETTINGS_URL),
            )
        )
        app.menu.add(rumps.separator)
    if snap.sync:
        _add_sync_section(app, snap.sync)
    name_w = _name_width(snap.infos)
    agents = getattr(app, "_rg_agents", {})
    summaries = [(info, repo_summary(info, name_w, agents)) for info in snap.infos]
    row_w = max(
        (_mono_attr(s, NSColor.labelColor()).size().width for _, s in summaries),
        default=0.0,
    ) + 2 * ROW_INSET + 12  # extra room for the submenu arrow
    for info, summary in summaries:
        pr = snap.prs.get(info.repo) if CONFIG.show_pr else None
        app.menu[f"{info.repo}:row"] = _repo_row_item(
            info.repo, summary, row_w, _ticket_for(info.branch, pr), pr,
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
            "Hide PRs" if CONFIG.show_pr else "Show PRs",
            callback=functools.partial(_on_toggle_pr, app),
        )
    )
    app.menu.add(rumps.MenuItem("Edit config", callback=_on_edit_config))
    gh_title = f"Open GitHub ({app_behind} behind)" if app_behind else "Open GitHub"
    app.menu.add(rumps.MenuItem(gh_title, callback=_on_open_github))
    app.menu.add(rumps.MenuItem("Pull (update)", callback=_on_pull_update))
    app.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))


def _open_in_app(app_name: str, repo: str) -> None:
    subprocess.run(["open", "-a", app_name, repo], capture_output=True)


def _latest_codex_thread(repo: str) -> str | None:
    """Read the latest user chat for this checkout from Codex's local index."""
    codex_home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    try:
        databases = [
            path
            for path in codex_home.glob("state_*.sqlite")
            if re.fullmatch(r"state_\d+\.sqlite", path.name)
        ]
        if not databases:
            return None
        # Only use the newest schema; older databases may contain stale chats.
        database = max(databases, key=lambda path: int(path.stem.split("_")[1]))
        checkout = Path(repo).expanduser().absolute()
        with closing(
            sqlite3.connect(
                database.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2
            )
        ) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)")
            }
            updated = (
                "COALESCE(updated_at_ms, updated_at * 1000)"
                if "updated_at_ms" in columns else "updated_at"
            )
            row = connection.execute(
                f"SELECT id FROM threads WHERE archived = 0 AND cwd IN (?, ?) "
                "AND source NOT LIKE '%subagent%' AND source != 'exec' "
                f"ORDER BY {updated} DESC, id DESC LIMIT 1",
                (str(checkout), str(checkout.resolve())),
            ).fetchone()
            return row[0] if row else None
    except (OSError, sqlite3.Error):
        # The index is an app internal: missing/changed schemas must not break
        # opening a repo, and reading must never create or migrate a database.
        return None


def _open_codex_repo(repo: str) -> None:
    thread_id = _latest_codex_thread(repo)
    if thread_id:
        url = "codex://threads/" + urllib.parse.quote(thread_id, safe="")
    else:
        url = "codex://new?" + urllib.parse.urlencode(
            {"path": str(Path(repo).expanduser().resolve())}
        )
    try:
        result = subprocess.run(
            ["open", url], capture_output=True, text=True, timeout=5
        )
        if result.returncode:
            print(
                f"repo-glance: could not open Codex: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"repo-glance: could not open Codex: {error}", file=sys.stderr)


def open_in_codex(repo: str) -> None:
    """Resolve the chat and launch Codex without blocking the menu's UI thread."""
    threading.Thread(target=_open_codex_repo, args=(repo,), daemon=True).start()


# Actions offered in a repo row's submenu / right-click context menu.
CONTEXT_ACTIONS = (
    ("Open in cmux", open_in_cmux),
    ("Open in WebStorm", functools.partial(_open_in_app, "WebStorm")),
    ("Open in Cursor", functools.partial(_open_in_app, "Cursor")),
    ("Open in Codex", open_in_codex),
)

JIRA_BROWSE_URL = "https://gameonsport.atlassian.net/browse/"

_TICKET_RE = re.compile(r"\b(GO-\d+)\b", re.IGNORECASE)


def _ticket_for(branch: str, pr: dict | None) -> str | None:
    """Jira ticket key referenced by the branch name or the PR title."""
    for text in (branch, (pr or {}).get("title", "")):
        m = _TICKET_RE.search(text or "")
        if m:
            return m.group(1).upper()
    return None


def _on_ctx_action(fn, repo: str, _item: rumps.MenuItem) -> None:
    fn(repo)


def _action_items(
    repo: str, ticket: str | None, pr: dict | None
) -> list[rumps.MenuItem]:
    items = [
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
    starred = Path(repo).name in CONFIG.important
    items.append(None)  # separator
    items.append(
        rumps.MenuItem(
            "Unmark important" if starred else "★ Mark important",
            callback=functools.partial(_on_toggle_important, repo),
        )
    )
    return items


def _add_items(menu: NSMenu, items: list) -> None:
    """Add rumps items to an NSMenu; None becomes a separator."""
    for item in items:
        menu.addItem_(NSMenuItem.separatorItem() if item is None else item._menuitem)


def _on_toggle_important(repo: str, _item: rumps.MenuItem) -> None:
    """Star/unstar a repo by rewriting the important key; the config-file
    fingerprint picks it up within a tick."""
    name = Path(repo).name
    _set_config_key("important", sorted(CONFIG.important ^ {name}))


def _row_submenu(
    repo: str, ticket: str | None, pr: dict | None
) -> tuple[NSMenu, list]:
    """The per-row actions submenu — hover the row to open it. A submenu runs
    inside the dropdown's own tracking session, which is the only way to show
    per-row actions while the dropdown stays open (see _popup_context_menu)."""
    sub = NSMenu.alloc().init()
    keep = _action_items(repo, ticket, pr)
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
            repo, getattr(view, "_rg_ticket", None), getattr(view, "_rg_pr", None)
        )
        _add_items(ctx, [header, None] + keep[1:])
        view._rg_ctx_keep = (ctx, keep)  # hold refs while the popup is up
        ctx.popUpMenuPositioningItem_atLocation_inView_(
            None, NSEvent.mouseLocation(), None
        )

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.0, False, show)


ROW_HEIGHT = 22.0
ROW_INSET = 14.0  # horizontal text inset, roughly native menu metrics


def _mono_attr(text: str, color: NSColor) -> NSAttributedString:
    return NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: _MONO_FONT, NSForegroundColorAttributeName: color}
    )


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
        open_in_cmux(view._rg_repo)


def _repo_row_view_class():
    """The custom NSView class for repo rows: draws its own hover highlight,
    opens cmux on left click, pops a context menu on right/ctrl click. Plain
    menu items can't do this — an NSMenuItem has a single action and never
    sees right clicks, so the row must handle its own mouse events.

    ObjC classes can't be re-registered, so the methods are thin delegates to
    module functions (which hot-reload). If the class body itself must change,
    bump the numeric suffix on the class name so a fresh class registers.
    """
    try:
        return objc.lookUpClass("RGRepoRowView2")
    except objc.error:
        pass

    class RGRepoRowView2(NSView):
        def updateTrackingAreas(self):
            objc.super(RGRepoRowView2, self).updateTrackingAreas()
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

    return RGRepoRowView2


def _on_row_select(repo: str, _item: rumps.MenuItem) -> None:
    open_in_cmux(repo)


IMPORTANT_COLOR = NSColor.systemOrangeColor


def _row_text(
    summary: str, repo: str, agent: str | None, highlighted: bool
) -> NSAttributedString:
    """The row's attributed text: the agent marker in its color, and for an
    important repo an orange star and a bold name. Highlighted rows stay all
    white (bold kept) so they read on the selection color."""
    text = NSMutableAttributedString.alloc().initWithAttributedString_(
        _mono_attr(summary, NSColor.whiteColor() if highlighted else NSColor.labelColor())
    )
    if agent and not highlighted:
        text.addAttribute_value_range_(
            NSForegroundColorAttributeName, AGENT_MARKS[agent][1](), (0, 1)
        )
    name = Path(repo).name
    if name in CONFIG.important:
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
    repo: str, summary: str, width: float, ticket: str | None, pr: dict | None,
    agent: str | None = None,
) -> rumps.MenuItem:
    view = _repo_row_view_class().alloc().initWithFrame_(
        NSMakeRect(0, 0, width, ROW_HEIGHT)
    )
    view._rg_repo = repo
    view._rg_ticket = ticket
    view._rg_pr = pr
    view._rg_text = _row_text(summary, repo, agent, highlighted=False)
    view._rg_text_hl = _row_text(summary, repo, agent, highlighted=True)
    # The callback keeps the item enabled under NSMenu's auto-enablement —
    # views inside a disabled item receive no mouse events. The view handles
    # clicks itself, so this only fires via keyboard selection.
    item = rumps.MenuItem(repo, callback=functools.partial(_on_row_select, repo))
    item._menuitem.setView_(view)
    sub, keep = _row_submenu(repo, ticket, pr)
    item._menuitem.setSubmenu_(sub)
    view._rg_keep = (sub, keep)
    return item


GITHUB_URL = "https://github.com/andersonku/repo-glance"
FETCH_SECONDS = 300  # at most one network fetch per this many seconds


def _commits_behind(app: rumps.App) -> int:
    """How many commits this checkout is behind upstream. The fetch is
    rate-limited (timestamp lives on the app object so it survives reloads);
    the rev-list itself is local and runs every refresh, so the count
    self-corrects right after a pull."""
    app_repo = str(Path(__file__).resolve().parent)
    last = getattr(app, "_rg_last_fetch", None)
    if last is None or time.monotonic() - last >= FETCH_SECONDS:
        try:
            subprocess.run(
                ["git", "-C", app_repo, "fetch", "--quiet"],
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass
        app._rg_last_fetch = time.monotonic()
    out = _git(app_repo, "rev-list", "--count", "HEAD..@{u}")
    return int(out) if out.isdigit() else 0


def _on_open_github(_: rumps.MenuItem) -> None:
    subprocess.run(["open", GITHUB_URL], capture_output=True)


def _on_toggle_pr(app: rumps.App, _: rumps.MenuItem) -> None:
    """Flip show_pr in the config file (so the choice persists), then refresh.
    The app is config-driven, so writing the file IS the toggle mechanism."""
    if _set_config_key("show_pr", not CONFIG.show_pr):
        app._on_refresh(None)


def _set_config_key(key: str, value) -> bool:
    """Rewrite `key = …` in the config file (a scalar, or an array that may
    span lines), appending it if absent. Returns False if the write failed."""
    line = f"{key} = {_toml_value(value)}"
    try:
        text = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else SAMPLE_CONFIG
        text, n = re.subn(
            rf"(?m)^{key}\s*=\s*(?:\[[^\]]*\]|.*$)", lambda _m: line, text
        )
        if not n:
            text += f"\n{KEY_COMMENTS[key]}\n{line}\n"
        CONFIG_PATH.write_text(text)
    except OSError:
        return False
    return True


def _on_edit_config(_: rumps.MenuItem) -> None:
    if not CONFIG_PATH.exists():
        _write_sample_config()
    subprocess.run(["open", "-t", str(CONFIG_PATH)], capture_output=True)


def _on_pull_update(_: rumps.MenuItem) -> None:
    """Pull the latest repo-glance code; this serves as a software update.

    No restart or explicit refresh needed: the pull changes this file's
    mtime, which trips the fingerprint, so the next tick hot-reloads the
    new code. Only changes to main.py still require a manual restart.
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


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _wip_usage_line(name: str, percent: int, resets, read_at, stale) -> str:
    line = f"- {name}: {percent}% used"
    if resets is not None:
        line += f", resets {resets.strftime('%Y-%m-%d %H:%M')}"
    if stale:
        line += f" (stale, as of {read_at.strftime('%Y-%m-%d %H:%M')})"
    return line


def _wip_markdown(snap: Snapshot, agents: dict[str, str]) -> str:
    """The menu's contents as Markdown, untruncated, for other agents."""
    home = str(Path.home())
    out = [
        "# WIP: local repo checkouts",
        "",
        f"Written by repo-glance at {snap.collected_at.strftime('%Y-%m-%d %H:%M:%S')}; "
        "rewritten on every refresh, so don't edit it.",
        "",
    ]
    out += [
        "Markers before a repo name: `◐` a cmux agent there is running; `✳` Claude "
        "there is waiting for the user's input; `★` the user marked it important.",
        "",
    ]
    if CONFIG.show_usage:
        out.append("## Usage")
        out.append("")
        out.append(_wip_usage_line("Claude (5-hour session)", *snap.usage)
                   if snap.usage else "- Claude: usage unavailable")
        if snap.codex:
            out.append(_wip_usage_line("Codex", *snap.codex))
        out.append("")
    if snap.sync:
        out += ["## Behind main (Node 24 upgrade)", ""]
        for label, (behind, ahead), extra in _sync_lines(snap.sync):
            line = f"- {label}: {behind} behind main, {ahead} ahead"
            out.append(line + (f", {extra}" if extra else ""))
        out.append("")
    out += [
        "## Repos",
        "",
        "| Repo | Path | Branch | Last commit | Uncommitted | PR | Ticket |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for info in snap.infos:
        path = info.repo.replace(home, "~", 1) if info.repo.startswith(home) else info.repo
        name = Path(info.repo).name
        state = agents.get(info.repo)
        marks = (f"{AGENT_MARKS[state][0]} " if state else "") + (
            "★ " if name in CONFIG.important else ""
        )
        name = marks + name
        if not info.is_git:
            out.append(f"| {name} | {path} | (not a git repo) | | | | |")
            continue
        last = f"{info.last_iso} ({_relative_days(info.last_iso)})" if info.last_iso else "-"
        pr = snap.prs.get(info.repo) if CONFIG.show_pr else None
        pr_cell = ""
        if pr:
            pr_cell = f"[#{pr['number']} {pr.get('title', '')}]({pr['url']})"
            extra = _pr_extra(pr)
            if extra:
                pr_cell += f" ({extra})"
        ticket = _ticket_for(info.branch, pr) or ""
        cells = (name, path, info.branch, last, info.uncommitted, pr_cell, ticket)
        out.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
    return "\n".join(out) + "\n"


def _write_wip(snap: Snapshot | None, agents: dict[str, str]) -> None:
    """Worker/poller thread. Atomic replace, so a reader never sees a
    half-written file; a per-thread temp name keeps the two writers apart."""
    if snap is None or not CONFIG.wip_file:
        return
    path = Path(CONFIG.wip_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(_wip_markdown(snap, agents))
    tmp.replace(path)


def _header(name_w: int = 12) -> str:
    return (
        f"{_gutter('')}{'REPO':<{name_w}}  "
        f"{'BRANCH':<{CONFIG.branch_width}}  "
        f"{'AGE':<{LAST_W}}  "
        f"DIRTY"
    )


def _print_cli_table(infos: list[RepoInfo], interval: int) -> None:
    name_w = _name_width(infos)
    print("\033[2J\033[H", end="")
    print(_header(name_w))
    print("-" * len(_header(name_w)))
    for info in infos:
        print(repo_summary(info, name_w))
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
        _print_cli_table(discover_repos(), CONFIG.refresh_seconds)
        # Snapshot after printing: the git calls may rewrite .git/index.
        state["fingerprint"] = _fingerprint()
        state["last_refresh"] = time.monotonic()
    return TICK_SECONDS
