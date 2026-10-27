"""repo-glance's app logic, split by concern. logic.py (the module main.py
hot-reloads) re-runs every module below on each tick, in this order — each
module may only import from modules listed before it."""

MODULES = (
    "config",
    "notices",
    "repos",
    "prs",
    "sync",
    "usage",
    "codex",
    "cmux",
    "snapshot",
    "fmt",
    "wip",
    "rows",
    "menu",
    "app",
    "cli",
)
