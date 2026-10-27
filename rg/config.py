"""Config (config.toml, hand-edited) and UI state (state.json, written by the
menu's toggles). Problems in either are collected on Config.errors and shown
in the menu instead of being raised, so a typo never takes the app down."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "repo-glance" / "config.toml"
# Owned by the app: the Mark-important and Hide/Show PRs toggles write it, so
# they never have to rewrite the hand-edited TOML.
STATE_PATH = CONFIG_PATH.with_name("state.json")

# The checkout this code runs from (rg/ is one level below the repo root).
APP_REPO = Path(__file__).resolve().parent.parent

# Diagnostics (slow menu opens); also visible in Console.app under Log Reports.
LOG_PATH = Path.home() / "Library" / "Logs" / "repo-glance.log"
LOG_MAX_BYTES = 512 * 1024  # rotated to repo-glance.log.old beyond this

# How often the launcher ticks. Each tick is a cheap stat-based change check;
# a full refresh runs only on change or every refresh_seconds.
TICK_SECONDS = 2
FETCH_SECONDS = 300  # at most one network fetch / PR lookup per this many seconds

DEFAULTS = {
    "scan_dirs": ["~/dev", "~/dev2"],
    "repo_pattern": r"playmaker\d*",
    "hide_pattern": [],
    "refresh_seconds": 60,
    "title": "RG",
    "sort": "name",
    "active_days": 30,
    "branch_width": 25,
    "show_usage": True,
    "claude_dirs": ["~/.claude"],
    "codex_label": "Codex",
    "icon": "",
    "wip_file": "~/.config/repo-glance/WIP.md",
}

# UI state and its defaults. Older builds kept these in config.toml; a value
# found there seeds state.json until the first toggle writes it.
STATE_DEFAULTS = {"important": [], "show_pr": True}

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
    "show_usage": (
        "# Show a first row with your Claude 5-hour session usage and when it\n"
        "# resets. Needs a Claude Code login; the token is read from the keychain,\n"
        "# so macOS asks for permission the first time. Also shows a Codex usage\n"
        "# row when ~/.codex session files are present (read locally, no network)."
    ),
    "claude_dirs": (
        "# Claude Code config dirs (CLAUDE_CONFIG_DIR values) to show usage for, one\n"
        "# segment each on the usage row. A list derives each label from the dir name\n"
        '# (~/.claude -> "Claude", ~/.claude-ak -> "Claude ak"); an inline table sets\n'
        '# short ones, e.g. claude_dirs = { A = "~/.claude", "A(2)" = "~/.claude-ak" }.'
    ),
    "codex_label": "# Label of the Codex segment in the usage row.",
    "icon": (
        "# Path to a .png/.svg file shown as the menu-bar icon instead of the title\n"
        '# text. "~" and $ENV_VARS are expanded; empty shows the title text.'
    ),
    "wip_file": (
        "# Markdown file rewritten on every refresh with the same info as the menu\n"
        "# (repos, branches, PRs, usage), for other agents to read; a .json twin is\n"
        "# written next to it. Empty disables both."
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
    repo_pattern: re.Pattern | None
    hide_pattern: re.Pattern | None
    refresh_seconds: int
    title: str
    sort: str
    active_days: int
    branch_width: int
    show_usage: bool
    claude_dirs: list[tuple[str, str]]  # (label, expanded config dir)
    codex_label: str
    icon: str
    wip_file: str
    # From state.json:
    show_pr: bool
    important: frozenset[str]
    # (file, message) for every problem found while loading. `fallback` means
    # config.toml couldn't be parsed at all and every key is at its default.
    errors: tuple[tuple[Path, str], ...] = ()
    fallback: bool = False
    state_ok: bool = True  # state.json is absent or readable, so safe to write


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
    """Combine a regex string or list into one anchored pattern; None if empty.
    Raises re.error for an invalid regex and TypeError for a non-string."""
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise TypeError("expected a regex string or a list of them")
    patterns = [p for p in patterns if p]
    if not patterns:
        return None
    combined = "|".join(f"(?:{p})" for p in patterns)
    return re.compile(f"^(?:{combined})$")


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def _str_list(value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TypeError("expected a list of strings")
    return value


KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_CLAUDE_DIR = "~/.claude"


def _is_default_dir(config_dir: str) -> bool:
    return os.path.normpath(os.path.expanduser(config_dir)) == os.path.normpath(
        os.path.expanduser(DEFAULT_CLAUDE_DIR)
    )


def keychain_service(config_dir: str) -> str:
    """The keychain item Claude Code keeps the OAuth token in for a config
    dir: the plain service name for ~/.claude, and for any other
    CLAUDE_CONFIG_DIR the same name suffixed with the first 8 hex chars of the
    sha256 of its absolute path (what Claude Code itself does)."""
    if _is_default_dir(config_dir):
        return KEYCHAIN_SERVICE
    path = os.path.normpath(os.path.expanduser(config_dir))
    return f"{KEYCHAIN_SERVICE}-{hashlib.sha256(path.encode()).hexdigest()[:8]}"


def claude_label(config_dir: str) -> str:
    """Row label derived from a config dir name: ~/.claude -> "Claude",
    ~/.claude-ak -> "Claude ak", ~/.work -> "work"."""
    if _is_default_dir(config_dir):
        return "Claude"
    name = os.path.basename(os.path.normpath(os.path.expanduser(config_dir))).lstrip(".")
    if name.lower().startswith("claude"):
        rest = name[len("claude"):].lstrip("-_ ")
        return f"Claude {rest}" if rest else "Claude"
    return name or "Claude"



def _claude_dirs(value) -> list[tuple[str, str]]:
    """[(label, dir)] from a list of dirs (labels derived) or a {label: dir}
    table."""
    if isinstance(value, dict):
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise TypeError("expected a table of label = dir strings")
        pairs = [(k, _expand(v)) for k, v in value.items()]
    else:
        pairs = [(claude_label(d), _expand(d)) for d in _str_list(value)]
    if not pairs:
        raise ValueError("expected at least one config dir")
    return pairs


def _as_int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected a whole number")
    return value


def _as_bool(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected true or false")
    return value


def _as_str(value) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value


def _as_sort(value) -> str:
    if value not in ("name", "oldest", "newest"):
        raise ValueError('expected "name", "oldest" or "newest"')
    return value


# key -> converter from the raw TOML value. A converter that raises is
# reported and the key falls back to its default.
_CONVERTERS = {
    "scan_dirs": lambda v: [Path(_expand(d)) for d in _str_list(v)],
    "repo_pattern": _full_match_pattern,
    "hide_pattern": _full_match_pattern,
    "refresh_seconds": _as_int,
    "title": _as_str,
    "sort": _as_sort,
    "active_days": _as_int,
    "branch_width": _as_int,
    "show_usage": _as_bool,
    "claude_dirs": _claude_dirs,
    "codex_label": _as_str,
    "icon": lambda v: _expand(_as_str(v)),
    "wip_file": lambda v: _expand(_as_str(v)),
}

_STATE_CONVERTERS = {
    "important": lambda v: frozenset(_str_list(v)),
    "show_pr": _as_bool,
}


def _read_toml(errors: list) -> dict | None:
    """The parsed config file, or None (with an error recorded) if it can't be
    read. A missing file is created from the sample."""
    if not CONFIG_PATH.exists():
        _write_sample_config()
        return {}
    try:
        with CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append((CONFIG_PATH, f"config.toml: {exc}. Using all defaults."))
        return None


def _read_state(errors: list) -> tuple[dict, bool]:
    """(raw state.json values, readable). Missing file -> ({}, True)."""
    if not STATE_PATH.exists():
        return {}, True
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append((STATE_PATH, f"state.json: {exc}. Toggles are disabled."))
        return {}, False
    if not isinstance(data, dict):
        errors.append((STATE_PATH, "state.json: expected a JSON object. Toggles are disabled."))
        return {}, False
    return data, True


def load_config() -> Config:
    """Load config.toml and state.json, falling back to defaults key by key.
    Never raises: every problem lands in Config.errors."""
    errors: list[tuple[Path, str]] = []
    parsed = _read_toml(errors)
    fallback = parsed is None
    if parsed:
        _append_missing_keys(parsed)
    raw = {**DEFAULTS, **(parsed or {})}

    values = {}
    for key, convert in _CONVERTERS.items():
        try:
            values[key] = convert(raw[key])
        except (TypeError, ValueError, re.error) as exc:
            errors.append((CONFIG_PATH, f"config.toml: {key}: {exc}. Using the default."))
            values[key] = convert(DEFAULTS[key])

    state, state_ok = _read_state(errors)
    for key, convert in _STATE_CONVERTERS.items():
        # state.json wins; a legacy config.toml value seeds it until then.
        source, value = (
            (STATE_PATH, state[key]) if key in state
            else (CONFIG_PATH, (parsed or {}).get(key, STATE_DEFAULTS[key]))
        )
        try:
            values[key] = convert(value)
        except (TypeError, ValueError) as exc:
            errors.append((source, f"{source.name}: {key}: {exc}. Using the default."))
            values[key] = convert(STATE_DEFAULTS[key])

    return Config(**values, errors=tuple(errors), fallback=fallback, state_ok=state_ok)


def save_state(**changes) -> str | None:
    """Merge `changes` into state.json (atomically) on top of the current
    effective values. Returns an error message instead of writing when that
    could lose data: state.json is unreadable, or config.toml failed to parse
    (its legacy values may not have seeded state.json yet)."""
    if not CONFIG.state_ok:
        return "state.json is unreadable; fix or delete it first."
    if CONFIG.fallback and not STATE_PATH.exists():
        return "config.toml has errors; fix it first."
    state = {"important": sorted(CONFIG.important), "show_pr": CONFIG.show_pr}
    state.update(changes)
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(f".{STATE_PATH.name}.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        return f"Couldn't write state.json: {exc}"
    return None


CONFIG = load_config()
