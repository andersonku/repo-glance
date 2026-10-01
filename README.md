# repo-glance

A macOS menu-bar app that shows the git status of each of your local repo checkouts at a glance — current branch, how long since the last commit, and the number of uncommitted changes. Click a repo to jump to its [cmux](https://cmux.com) workspace.

It discovers repos by scanning folders you configure (e.g. `~/dev`, `~/dev2`) for directory names matching one or more regexes (e.g. `playmaker\d*`, `fastbreak\d*`).

![repo-glance menu-bar dropdown](docs/screenshot.png)

---

## Install with an AI agent

Paste this into an agent (e.g. Claude Code) running in a terminal on your Mac:

```
Install and configure the repo-glance menu-bar app from this repo for me.

1. Make sure `uv` is installed (https://docs.astral.sh/uv/). If not, install it.
2. From the repo root, run `uv sync` to create the venv and install dependencies.
3. Run `uv run main.py --cli` once to generate the default config at
   ~/.config/repo-glance/config.toml, then Ctrl-C to stop.
4. Ask me which folders my repos live in and what naming pattern(s) they follow,
   then edit ~/.config/repo-glance/config.toml accordingly:
     - scan_dirs: the folders to scan (supports ~ and $ENV_VARS)
     - repo_pattern: a regex string, or a list of regexes; a directory is shown
       if its name FULLY matches any of them
   Leave refresh_seconds and title at their defaults unless I ask otherwise.
5. Launch the menu-bar app with `uv run main.py --no-cli` and confirm my repos
   appear in the menu-bar dropdown.
```

---

## Requirements

- **macOS** (it's a menu-bar app; uses `rumps` + AppKit)
- **Python 3.11+** (uses the stdlib `tomllib`)
- [**uv**](https://docs.astral.sh/uv/) for dependency management
- *(optional)* [**cmux**](https://cmux.com) — only needed for the click-to-open-workspace feature

## Manual install

```sh
uv sync                  # create .venv and install dependencies
uv run main.py           # run it
```

## Running

| Command | Mode |
| --- | --- |
| `uv run main.py` | Menu-bar app **and** prints a refreshing table to the terminal |
| `uv run main.py --no-cli` | Menu-bar app only (no terminal output) — best for running in the background |
| `uv run main.py --cli` | Terminal table only, no menu-bar icon (handy over SSH / for a first run) |

The menu bar shows a short title (default `RG`); click it to see your Claude/Codex usage, the per-repo status (with a PR line under each repo that has one), a "Refresh now" item, "Hide/Show PRs", "Edit config", "Open GitHub" (with how many commits behind the app's own checkout is), "Pull (update)" (updates the app itself via `git pull`; hot reload applies it within seconds), and "Quit".

### Usage row (Claude + Codex)

The first row shows how much of your current Claude 5-hour session limit you've used and when it resets — the same numbers as Claude Code's own `/usage` — and, when `~/.codex/sessions` exists, your Codex CLI usage on the same line:

```
Claude ███░░░░░░░  28% resets 20:00  │  Codex ███░░░░░░░  28% resets 15/09
```

Click it to open your Claude usage settings. Codex usage is read locally from the newest `rollout-*.jsonl` session file (`payload.rate_limits.primary`) — no network, no auth. Codex reports whichever limit it considers primary (sometimes the 5-hour window, sometimes the weekly one), so the reset shows a time or a date accordingly; `0%` with no reset means the last recorded window has expired and Codex hasn't been used since.

It reads Claude Code's OAuth token from the login keychain, so **macOS asks for permission the first time** (choose "Always Allow" to avoid being asked again). repo-glance only reads the token; refreshing it stays Claude Code's job, so if the token has expired the row reads `usage unavailable` until Claude Code next runs. Set `show_usage = false` in the config to hide the row.

The usage endpoint is tightly rate-limited, so it's polled at most once every 5 minutes regardless of how often the rest of the menu refreshes. If a poll fails, the last good reading stays on screen with an "as of" note rather than disappearing:

```
Claude  ███░░░░░░░   31%   resets 12:35   (as of 08:53)
```

`usage unavailable` therefore means either that no reading has succeeded yet, or that the 5-hour window rolled over while polls were failing (a percentage from the previous window would be wrong, so it's dropped rather than shown stale).

### Behind-main section (temporary)

Under the usage row, two hard-coded lines track the Node 24 upgrade (`SYNC_*` constants in `logic.py`): how far the remote PR branch and the local `playmaker9` checkout are behind `origin/main` — the cue that it's time to rebase.

```
PR #10405 (remote)       0 behind main, 528 ahead  [CI passed]
playmaker9 (local)       0 behind main, 528 ahead
```

The PR line also shows its CI state (`CI passed` / `CI running` / `CI failed`, via `gh`). `git fetch origin main <branch>` and the `gh pr view` run at most every 5 minutes (never a full fetch, never a credential prompt); the counts are computed locally on every refresh, so they update right after a rebase/push. Clicking the PR line opens it on GitHub; the local line opens the checkout in cmux. Delete `_add_sync_section` and the `SYNC_*` constants once the project lands.

### Hot reload

`main.py` is a thin launcher; all app logic lives in `logic.py`, which is reloaded on every tick (~2 s). Edits to `logic.py` take effect within seconds without restarting the app — only changes to `main.py` itself need a restart. If you save `logic.py` with an error in it, the app keeps running the last good version and logs the error to stderr.

### Change detection

The app ticks every 2 seconds, but each tick only stats a handful of files — the config file and each repo's `.git/HEAD`, `.git/index`, `.git/packed-refs`, and `.git/logs/HEAD`. A full refresh (git commands, menu rebuild) runs only when one of those changed, so commits, branch switches, staging, pulls, and config edits show up within ~2 s. Unstaged working-tree edits don't touch those files; they're picked up by the unconditional full refresh every `refresh_seconds`.

A refresh never blocks the UI: all git, `gh`, and network calls run on a background worker thread that produces a snapshot, and the main thread only turns the finished snapshot into menu items (a few milliseconds). A cold refresh — usage poll, PR lookups, and fetches all expire on the same 5-minute cadence — takes 10–15 s in the background; the previous menu stays up and responsive meanwhile. At startup the menu shows `Loading…` until the first snapshot lands.

## Configuration

On first run, a commented sample is written to `~/.config/repo-glance/config.toml`. Changes to the file are **detected within ~2 s**, so edits (including the menu title and refresh interval) take effect almost immediately — no restart needed.

| Key | Default | Description |
| --- | --- | --- |
| `scan_dirs` | `["~/dev", "~/dev2"]` | Folders to scan. `~` and `$ENV_VARS` are expanded. |
| `repo_pattern` | `"playmaker\\d*"` | A regex **string**, or a **list** of regexes. A directory is shown if its name *fully* matches any of them. |
| `hide_pattern` | `[]` | Regex string or list; a directory whose name *fully* matches any of them is hidden even if `repo_pattern` matches it. |
| `refresh_seconds` | `60` | Seconds between unconditional full refreshes. Git and config changes are detected within ~2 s regardless; this backstop also catches unstaged working-tree edits. |
| `title` | `"RG"` | The menu-bar title. |
| `sort` | `"name"` | Listing order: `"name"` (grouped by scan dir), `"oldest"` (oldest last commit first), or `"newest"` (most recent first). |
| `active_days` | `30` | Hide repos whose last commit is older than this. `0` shows all. |
| `branch_width` | `25` | Branch column width; longer names are truncated with `...`. |
| `show_pr` | `true` | PR line under each repo (needs an authed `gh`). Also toggled by the Hide/Show PRs menu item, which rewrites this key. |
| `show_usage` | `true` | The Claude/Codex usage row. |
| `icon` | `""` | Path to a `.png`/`.svg` menu-bar icon instead of the title text (candidates in `docs/icons/`). |
| `wip_file` | `"~/.config/repo-glance/WIP.md"` | Markdown file rewritten after every refresh with the menu's info (usage, behind-main, and a repo table with full paths, branches, PRs, and tickets, nothing truncated) so other agents can read it. Written atomically. Empty disables. |
| `important` | `[]` | Repo directory names shown with a `★` in front. Toggled per repo by **Mark/Unmark important** in the row's context menu, which rewrites this key. |

New keys added by an update are appended to an existing config file as commented blocks (`_append_missing_keys`), so they're discoverable without re-creating the file.

Example with multiple patterns, oldest commits first:

```toml
scan_dirs = ["~/dev", "~/dev2", "~/work/$USER/projects"]
repo_pattern = ["playmaker\\d*", "fastbreak\\d*"]
refresh_seconds = 60
title = "RG"
sort = "oldest"
```

Notes:
- Patterns are matched against the directory **name only**, anchored as a full match (`^(?:…)$`), so `myrepo\d*` will not match `myrepository`.
- A malformed config is ignored with a warning on stderr — the app falls back to defaults rather than crashing.

## Repo rows: click, hover, right-click

Each repo row is `name  branch  age  ~dirty ▸`, optionally prefixed by markers: a green `◐` when an agent in one of that repo's [cmux](https://cmux.com) workspaces is running, otherwise a red `✳` when Claude there sent "Claude is waiting for your input" and you haven't replied since (`AGENT_MARKS`), and an orange `★` plus a **bold** name when you've marked it important. Each marker column only appears while some repo uses it. Hovering it opens a submenu of actions; **left-click** runs the first one (cmux) directly.

| Action | What it does |
| --- | --- |
| Open in cmux | Focuses the existing [cmux](https://cmux.com) workspace whose cwd **is** that repo, or opens a new one; brings cmux to the front. No-op if cmux isn't installed. |
| Open in WebStorm / Open in Cursor | `open -a <App> <repo>`. Add more editors by appending to `CONTEXT_ACTIONS` in `logic.py`. |
| Open in Codex | Opens the most recently updated, unarchived user chat for that checkout, or a new chat with the checkout selected. Reads the newest `state_*.sqlite` in `$CODEX_HOME` (default `~/.codex`) in read-only mode; excludes internal subagents and non-interactive sessions. If the index is missing or incompatible, opens a new chat. Requires the desktop app's `codex://` URL handler. |
| Open PR #N | Only when the branch has a PR (via `gh`). |
| Open GO-XXXXX | Only when a Jira key is found in the branch name or PR title (`_TICKET_RE`, `JIRA_BROWSE_URL`). |
| ★ Mark important / Unmark important | Adds or removes the repo's name in the `important` config key. Starred rows get a `★` before the name (and in `WIP.md`). |

**Right-click / ctrl-click** closes the dropdown and pops the same actions as a context menu at the cursor. (It has to close the dropdown first — see *AppKit constraints* below.)

The PR line under a repo (`↳ PR #N  title  [CI failed, Approved]`) opens the PR; quiet states (open, CI green, no review yet) show no bracket.

## Development

Everything below is what you need to pick this project up in a fresh session.

### Layout

| File | Role |
| --- | --- |
| `main.py` | Thin launcher: owns the Cocoa event loop, the status item, and the 2 s `rumps.Timer`. Reloads `logic` on every tick and calls `logic.tick(app)`. **Needs a restart to change.** |
| `logic.py` | Everything else — config, repo discovery, data collection, menu rendering, row views, usage lookups. Hot-reloaded. |
| `run.bash` | `uv run main.py`. |
| `docs/` | README screenshot and menu-bar icon candidates. |

The app is usually already running while you edit. Save `logic.py` and the change is live within ~2 s; a syntax/import error keeps the previous version running and prints a traceback to the launcher's stderr. Check with `pgrep -fl main.py`.

### Threading model — never block the main thread

`tick()` runs on the main thread (the one that draws the menu bar), so anything slow there freezes the whole app. The split is:

- **Worker thread** — `_collect(app)` builds a `Snapshot` (repos, PRs, usage, Codex, sync section, self-update count). Every `git`, `gh`, and network call lives here. A cold pass takes 10–15 s.
- **Main thread** — `tick()` renders a pending snapshot via `_build_menu(app, snap)` (~25 ms) and starts a new worker when the fingerprint changed or `refresh_seconds` elapsed. It never starts a second worker while one is alive.
- `refresh()` (startup, Refresh now, Hide/Show PRs) reloads config, re-renders the *existing* snapshot immediately so toggles feel instant, then starts a worker.

Rule of thumb: if a function calls `subprocess` or `urllib`, it belongs in `_collect` or something it calls, and its result goes into `Snapshot`. `_build_menu` must stay pure rendering. Slow lookups are rate-limited to `FETCH_SECONDS` / `USAGE_SECONDS` (5 min) with timestamps cached on the app object; local `rev-list` counts run every pass so they self-correct immediately after a pull/rebase.

### State lives on the `app` object

Module globals are wiped on every reload, so anything that must survive is an `app._rg_*` attribute: `_rg_snapshot`, `_rg_pending`, `_rg_worker`, `_rg_fingerprint`, `_rg_last_refresh`, `_rg_usage`, `_rg_pr_cache`, `_rg_sync_fetched`, `_rg_sync_pr`, `_rg_last_fetch`, `_rg_agents`, `_rg_cmux_worker`, `_rg_cmux_polled`. Read them with `getattr(app, name, default)` — a freshly started app has none of them, and an older build may have left a different shape (see `_usage` for the defensive pattern).

### Hot-reload constraints

- `main.py` must never `from logic import x`; always go through the module attribute.
- ObjC classes can't be re-registered. The row view class is created through `_repo_row_view_class()`, which returns the existing class via `objc.lookUpClass` if it's already registered. Its methods are thin delegates to module functions (`_row_draw`, `_row_event`), so **behaviour** hot-reloads; if the class body itself must change (new method/ivar), bump the class-name suffix (`RGRepoRowView2` → `3`).
- The worker may be running the *previous* module's `_collect` while the main thread renders with the new `_build_menu`. Snapshots are plain data, so this is fine as long as you don't rename `Snapshot` fields without expecting one failed render (`_render` clears `_rg_pending` first so it can't loop).

### AppKit constraints learned the hard way

These cost a lot of trial and error; don't re-fight them.

- Plain `NSMenuItem`s **never fire on right-mouse-up**, and any item activation closes the dropdown. Hence rows are custom `NSView`s (`setView_`) that get raw mouse events.
- Items with no callback are auto-disabled by rumps, and **views inside disabled items get no mouse events** — every row keeps a callback (`_on_row_select`).
- A second menu-tracking session **cannot start while the dropdown's session is active**: `popUpContextMenu_withEvent_forView_` silently no-ops. The only way to show per-row actions with the dropdown open is a **submenu** (`setSubmenu_`), which runs inside the same session. The right-click fallback must `cancelTracking()` the dropdown, then pop the menu from a 0 s `NSTimer` so the tracking run loop has unwound.
- View-based items draw nothing themselves: highlight (`NSTrackingArea` enter/exit → `setNeedsDisplay_`), text, and the submenu arrow are all drawn in `_row_draw`. Row width is computed from the widest summary string (`_mono_attr(...).size()`).
- Keep strong references to submenus/context menus on the view (`_rg_keep`, `_rg_ctx_keep`) or they're collected while open.
- rumps keys menu items by title and silently drops duplicates — use explicit keys (`app.menu["<repo>:row"] = item`) for anything that can repeat.

### Data sources

| What | How | Cadence |
| --- | --- | --- |
| Repo status | `git log/rev-parse/status` per repo, 16-thread pool | every pass |
| PRs | `gh pr view --json …` in each repo (resolves the branch itself) | 5 min per (repo, branch) |
| Claude usage | `GET https://api.anthropic.com/api/oauth/usage` with the OAuth token from keychain item `Claude Code-credentials` (`security find-generic-password`) | 5 min; a 429 keeps the last reading flagged stale |
| Codex usage | newest `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, last 64 KB, line containing `"rate_limits"` | every pass (local) |
| cmux agent state (`◐` / `✳`) | `cmux rpc workspace.list` + `cmux --json list-notifications`, then `cmux --json workspace status --workspace <id>` for each workspace whose cwd is at or under a listed repo (8 in parallel). **Running**: `signals.any_agent_running`. **Waiting**: the newest "Claude is waiting for your input" notification is newer than the workspace's `latest_submitted_at` (read state is ignored, because cmux keeps notifications after they're read). Running wins per repo. Runs on its own background thread (`_poll_cmux`) | every 5 s (`CMUX_POLL_SECONDS`); the menu re-renders and `WIP.md` is rewritten only when a state changes |
| Self-update count | `git fetch` in this repo + `rev-list HEAD..@{u}` | 5 min fetch, count every pass |

`gh` lives in `/opt/homebrew/bin`, which isn't on PATH when launched as an app — see `GH_BIN`.

### Testing without the GUI

`logic.py` imports AppKit but most functions run headless. A fake app object is enough to exercise the whole collect → render cycle from a shell:

```sh
.venv/bin/python - <<'PY'
import time, logic
class M(dict):
    def add(self, i): self[len(self)] = i
class A: pass
app = A(); app.menu = M(); app.title = None; app.icon = None; app._on_refresh = lambda *_: None
logic.refresh(app, also_print=False)          # starts the worker, renders "Loading…"
while app._rg_worker.is_alive(): time.sleep(1)
logic.tick(app, also_print=False)             # renders the snapshot
print([getattr(v, "title", "—") for v in app.menu.values()])
PY
```

`uv run main.py --cli` prints the table without a menu bar. There is no test suite.

### Temporary code

The **behind-main section** (`SYNC_REPO`, `SYNC_BRANCH`, `SYNC_PR_URL`, `_fetch_refs`, `_behind_ahead`, `_sync_data`, `_add_sync_section`, the `sync=` field of `Snapshot`) is hard-coded for one project and should be deleted when PR #10405 lands.

## Run at login (optional)

To keep the menu-bar app running across reboots, create a LaunchAgent. Save the following as `~/Library/LaunchAgents/com.user.repo-glance.plist`, replacing `/ABSOLUTE/PATH/TO/repo-glance` with this repo's path and the `uv` path with the output of `which uv`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>com.user.repo-glance</string>
  <key>ProgramArguments</key>
  <array>
    <string>/ABSOLUTE/PATH/TO/uv</string>
    <string>run</string>
    <string>main.py</string>
    <string>--no-cli</string>
  </array>
  <key>WorkingDirectory</key> <string>/ABSOLUTE/PATH/TO/repo-glance</string>
  <key>RunAtLoad</key>        <true/>
</dict>
</plist>
```

Then load it:

```sh
launchctl load ~/Library/LaunchAgents/com.user.repo-glance.plist
```
