"""Claude (OAuth usage endpoint) and Codex (local session files) usage."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config as cfg
from .config import DEFAULT_CLAUDE_DIR, keychain_service

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_SETTINGS_URL = "https://claude.ai/settings/usage"
# The usage endpoint allows roughly one call every few minutes; a second call
# 20s after a successful one already 429s. Its 429 carries "Retry-After: 0" and
# no reset timestamp, so there is nothing useful to back off against — hence a
# fixed interval, and a failed poll just leaves the last reading up as stale.
USAGE_SECONDS = 300
USAGE_BAR_W = 10


def _oauth_token(config_dir: str = DEFAULT_CLAUDE_DIR) -> str | None:
    """Claude Code's OAuth access token for a config dir, read from the login
    keychain (or the dir's .credentials.json where Claude Code fell back to a
    file). None if it's missing or already expired — Claude Code itself owns
    refreshing it, so a stale token just means no usage row until it next
    runs."""
    raw = None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", keychain_service(config_dir), "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            raw = result.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    if raw is None:
        try:
            raw = (Path(os.path.expanduser(config_dir)) / ".credentials.json").read_text()
        except OSError:
            return None
    try:
        oauth = json.loads(raw)["claudeAiOauth"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 <= time.time():
        return None
    return oauth.get("accessToken")


def _usage_lookup(config_dir: str = DEFAULT_CLAUDE_DIR) -> tuple[int, datetime] | None:
    """(percent used, local reset time) for the current 5-hour session limit —
    the same numbers Claude Code's own /usage shows. None on any failure."""
    token = _oauth_token(config_dir)
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


def _usage(app, config_dir: str = DEFAULT_CLAUDE_DIR) -> tuple[int, datetime, datetime, bool] | None:
    """(percent, reset time, when it was read, stale) for the 5-hour session
    limit of the account logged in under `config_dir` (a CLAUDE_CONFIG_DIR).

    The endpoint is tightly rate-limited, so it's polled at most once per
    USAGE_SECONDS and a failed poll (429, expired token, no network) keeps
    returning the last good reading, flagged stale, rather than blanking the
    row — the same fallback Claude Code's own /usage uses. `stale` tracks the
    last poll's outcome rather than the reading's age, so a 429 is reflected
    immediately instead of after the reading happens to get old enough. State
    lives on the app object (one entry per config dir) so it survives module
    reloads.
    """
    states = getattr(app, "_rg_usage_by_dir", None)
    if not isinstance(states, dict):
        states = app._rg_usage_by_dir = {}
    key = keychain_service(config_dir)
    state = states.get(key, (None, None, True))
    if len(state) != 3 or (state[1] is not None and len(state[1]) != 3):
        state = (None, None, True)  # state from an older build; re-poll
    attempted, reading, polled_ok = state
    now = time.monotonic()
    if attempted is None or now - attempted >= USAGE_SECONDS:
        fresh = _usage_lookup(config_dir)
        polled_ok = fresh is not None
        if fresh:
            reading = (*fresh, datetime.now().astimezone())
        attempted = now
        states[key] = (attempted, reading, polled_ok)
    if not reading:
        return None
    # Once the window it described has rolled over, a stale percent is not
    # merely old, it's wrong — drop it rather than show a number from the
    # previous session.
    if reading[1] <= datetime.now().astimezone():
        states[key] = (attempted, None, polled_ok)
        return None
    return (*reading, not polled_ok)


def claude_usages(app) -> list[tuple[str, tuple | None]]:
    """(label, reading-or-None) for every configured Claude config dir."""
    return [(label, _usage(app, d)) for label, d in cfg.CONFIG.claude_dirs]


def usage_summary(
    name: str, percent: int, resets: datetime | None, read_at: datetime, stale: bool
) -> str:
    """One compact segment of the shared usage row."""
    filled = min(USAGE_BAR_W, max(0, round(percent / 100 * USAGE_BAR_W)))
    bar = "█" * filled + "░" * (USAGE_BAR_W - filled)
    now = datetime.now().astimezone()
    seg = f"{name} {bar} {percent:>3}%"
    if resets is not None:
        # The reset time follows the percentage (a bare time or date, to keep
        # the row narrow): same-day resets show a time; longer windows (Codex
        # is weekly) a date.
        fmt = "%H:%M" if (resets - now).total_seconds() < 86400 else "%d/%m"
        seg += f" {resets.strftime(fmt)}"
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
