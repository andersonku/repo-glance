"""Hot-reload entry point. main.py reloads this module on every tick; this
module in turn re-runs the rg submodules that changed on disk — and every
module after the first changed one in rg.MODULES, since those may hold
`from` bindings into it — so an edit goes live within a tick without a
restart. Unchanged ticks only stat the files (well under a millisecond).

Every changed source is compiled before anything is reloaded: a syntax error
in any module leaves the whole app on its last good version (the error goes
to stderr via main.py) instead of half the modules running new code."""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import rg

_PACKAGE_DIR = Path(rg.__file__).resolve().parent

# module name -> mtime_ns of the source it was last loaded from. setdefault
# because main.py reloads this module every tick, and reload re-runs this
# code in the same namespace: the dict must survive that.
_LOADED: dict[str, int | None] = globals().setdefault("_LOADED", {})


def _check_sources(paths: list[Path]) -> None:
    """Compile each file; raises SyntaxError before any module is touched."""
    for path in paths:
        compile(path.read_text(), str(path), "exec")


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _to_reload(modules, mtimes: dict, loaded: dict, imported) -> list[str]:
    """Modules to re-run, in order: from the first one whose source changed
    (or that was never imported) through the end of the list."""
    for i, name in enumerate(modules):
        if name not in imported or name not in loaded or mtimes.get(name) != loaded[name]:
            return list(modules[i:])
    return []


def _reload_package() -> None:
    started = time.perf_counter()
    init = _PACKAGE_DIR / "__init__.py"
    init_changed = "__init__" not in _LOADED or _mtime(init) != _LOADED["__init__"]
    if init_changed:
        _check_sources([init])
        importlib.reload(rg)  # picks up changes to rg.MODULES
        _LOADED["__init__"] = _mtime(init)
    paths = {name: _PACKAGE_DIR / f"{name}.py" for name in rg.MODULES}
    mtimes = {name: _mtime(path) for name, path in paths.items()}
    imported = {name for name in rg.MODULES if f"rg.{name}" in sys.modules}
    names = list(rg.MODULES) if init_changed else _to_reload(rg.MODULES, mtimes, _LOADED, imported)
    if names:
        _check_sources([paths[name] for name in names])
        for name in names:
            full = f"rg.{name}"
            if full in sys.modules:
                importlib.reload(sys.modules[full])
            else:
                importlib.import_module(full)
            _LOADED[name] = mtimes[name]
    # Read by the slow-menu-open log (rg.menu) for context.
    rg.last_reload = (time.monotonic(), (time.perf_counter() - started) * 1000, len(names))


_reload_package()

# The launcher's entry points.
from rg.app import TICK_SECONDS, refresh, tick  # noqa: E402,F401
from rg.cli import cli_tick  # noqa: E402,F401

# Row views built before the split (class RGRepoRowView2) resolve these names
# in this module; keep them so an already-open menu draws until it's rebuilt.
from rg.rows import _row_draw, _row_event  # noqa: E402,F401
