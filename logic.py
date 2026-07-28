"""All app logic for repo-glance. Reloaded by main.py on every tick,
so edits to this file take effect without restarting the app."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import rumps
from AppKit import NSAttributedString, NSFont, NSFontAttributeName, NSImage

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
    "active_days": 30,
    "branch_width": 25,
    "show_pr": True,
    "show_usage": True,
    "icon": "",
}

# Comment block above each key — used for the sample config and for the
# blocks appended to existing config files when an update adds new keys.
KEY_COMMENTS = {
    "scan_dirs": '# Folders to scan for repo checkouts. "~" and $ENV_VARS are expanded.',
    "repo_pattern": (
        "# A directory is shown if its name FULLY matches any of these regexes.\n"
        '# A list also works: repo_pattern = ["playmaker\\\\d*", "fastbreak\\\\d*"]'
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
        "# so macOS asks for permission the first time."
    ),
    "icon": (
        "# Path to a .png/.svg file shown as the menu-bar icon instead of the title\n"
        '# text. "~" and $ENV_VARS are expanded; empty shows the title text.'
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
    refresh_seconds: int
    title: str
    sort: str
    active_days: int
    branch_width: int
    show_pr: bool
    show_usage: bool
    icon: str


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
        active_days=int(raw["active_days"]),
        branch_width=int(raw["branch_width"]),
        show_pr=bool(raw["show_pr"]),
        show_usage=bool(raw["show_usage"]),
        icon=os.path.expandvars(os.path.expanduser(str(raw["icon"]))),
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


def _candidate_repos() -> list[str]:
    """Scan configured dirs for repos matching the pattern. Stat-only, no git calls."""
    repos: list[str] = []
    for root in CONFIG.scan_dirs:
        if not root.is_dir():
            continue
        matches = [
            p for p in root.iterdir() if p.is_dir() and CONFIG.repo_pattern.match(p.name)
        ]
        repos.extend(str(p) for p in sorted(matches, key=lambda p: p.name))
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
        infos.sort(key=lambda i: Path(i.repo).name)
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


LAST_W = 7
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
    return f"{days}d"


def _name_width(infos: list[RepoInfo]) -> int:
    return max((len(Path(i.repo).name) for i in infos), default=12)


def repo_summary(info: RepoInfo, name_w: int = 12) -> str:
    name = Path(info.repo).name
    if not info.is_git:
        return f"{name:<{name_w}}  (not a git repo)"
    branch_w = CONFIG.branch_width
    branch = info.branch
    if len(branch) > branch_w:
        branch = branch[: branch_w - 3] + "..."
    last = _relative_days(info.last_iso) if info.last_iso else "-"
    dirty = f"~{info.uncommitted}" if info.uncommitted else ""
    return (
        f"{name:<{name_w}}  "
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
    """Called by the launcher every TICK_SECONDS, after this module has been
    reloaded. Runs a full refresh only when the fingerprint changed or
    refresh_seconds has elapsed since the last one."""
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


def refresh(app: rumps.App, also_print: bool) -> None:
    """Reload config, update the menu-bar app, and optionally print the CLI table.

    Unconditional — used at startup, by the Refresh-now menu item, and by
    tick() when a change is detected.
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
    _build_menu(app)
    # Snapshot AFTER the git calls above: `git status` may itself rewrite
    # .git/index, which must not register as a new change on the next tick.
    # State lives on the app object because module globals are wiped by reload.
    app._rg_fingerprint = _fingerprint()
    app._rg_last_refresh = time.monotonic()
    if also_print:
        _print_cli_table(CONFIG.refresh_seconds)


# gh lives in /opt/homebrew/bin, which isn't on PATH when launched as an app.
GH_BIN = shutil.which("gh") or "/opt/homebrew/bin/gh"

PR_TITLE_W = 50  # PR titles longer than this are truncated with "..."


def _pr_lookup(repo: str) -> dict | None:
    """PR for the repo's current branch via `gh pr view` (resolves the branch
    itself). None if there is no PR, gh is missing, or gh isn't authed."""
    try:
        result = subprocess.run(
            [GH_BIN, "pr", "view", "--json", "number,url,title"],
            cwd=repo, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None


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


def usage_summary(percent: int, resets: datetime, read_at: datetime, stale: bool) -> str:
    filled = min(USAGE_BAR_W, max(0, round(percent / 100 * USAGE_BAR_W)))
    bar = "█" * filled + "░" * (USAGE_BAR_W - filled)
    row = f"Claude  {bar}  {percent:>3}%   resets {resets.strftime('%H:%M')}"
    if stale:
        row += f"   (as of {read_at.strftime('%H:%M')})"
    return row


def _build_menu(app: rumps.App) -> None:
    app.menu.clear()
    if CONFIG.show_usage:
        usage = _usage(app)
        app.menu.add(
            _mono_item(
                usage_summary(*usage) if usage else "Claude  usage unavailable",
                callback=functools.partial(_on_open_url, USAGE_SETTINGS_URL),
            )
        )
        app.menu.add(rumps.separator)
    infos = discover_repos()
    name_w = _name_width(infos)
    prs = _pr_map(app, infos) if CONFIG.show_pr else {}
    for info in infos:
        app.menu.add(
            _mono_item(
                repo_summary(info, name_w),
                callback=functools.partial(_on_repo_click, info.repo),
            )
        )
        pr = prs.get(info.repo)
        if pr:
            # Explicit key: rumps keys items by title and silently drops
            # duplicates, which would hide the PR row of a second checkout
            # on the same branch.
            title = pr.get("title", "")
            if len(title) > PR_TITLE_W:
                title = title[: PR_TITLE_W - 3] + "..."
            app.menu[f"{info.repo}:pr"] = _mono_item(
                f"   ↳ PR #{pr['number']}  {title}",
                callback=functools.partial(_on_open_url, pr["url"]),
            )
    app.menu.add(rumps.separator)
    app.menu.add(
        rumps.MenuItem(f"Refreshed {datetime.now().strftime('%H:%M:%S')}")
    )
    app.menu.add(rumps.MenuItem("Refresh now", callback=app._on_refresh))
    app.menu.add(
        rumps.MenuItem(
            "Hide PRs" if CONFIG.show_pr else "Show PRs",
            callback=functools.partial(_on_toggle_pr, app),
        )
    )
    app.menu.add(rumps.MenuItem("Edit config", callback=_on_edit_config))
    behind = _commits_behind(app)
    gh_title = f"Open GitHub ({behind} behind)" if behind else "Open GitHub"
    app.menu.add(rumps.MenuItem(gh_title, callback=_on_open_github))
    app.menu.add(rumps.MenuItem("Pull (update)", callback=_on_pull_update))
    app.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))


def _on_repo_click(repo: str, _: rumps.MenuItem) -> None:
    open_in_cmux(repo)


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
    new = "false" if CONFIG.show_pr else "true"
    try:
        text = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else SAMPLE_CONFIG
        text, n = re.subn(r"(?m)^show_pr\s*=.*$", f"show_pr = {new}", text)
        if not n:
            text += f"\n{KEY_COMMENTS['show_pr']}\nshow_pr = {new}\n"
        CONFIG_PATH.write_text(text)
    except OSError:
        return
    app._on_refresh(None)


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


def _header(name_w: int = 12) -> str:
    return (
        f"{'REPO':<{name_w}}  "
        f"{'BRANCH':<{CONFIG.branch_width}}  "
        f"{'AGE':<{LAST_W}}  "
        f"DIRTY"
    )


def _print_cli_table(interval: int = CONFIG.refresh_seconds) -> None:
    infos = discover_repos()
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
        _print_cli_table(CONFIG.refresh_seconds)
        # Snapshot after printing: the git calls may rewrite .git/index.
        state["fingerprint"] = _fingerprint()
        state["last_refresh"] = time.monotonic()
    return TICK_SECONDS
