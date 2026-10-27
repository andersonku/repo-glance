"""Shared test helpers: point config/state at a temp dir and swap CONFIG."""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path
from unittest.mock import patch

from rg import config as cfg


def isolate_config(case) -> Path:
    """Redirect CONFIG_PATH/STATE_PATH into a fresh temp dir for this test,
    and restore the real CONFIG afterwards. Returns the temp dir."""
    temp = tempfile.TemporaryDirectory(prefix="rg-test-")
    case.addCleanup(temp.cleanup)
    root = Path(temp.name)
    for name, value in (("CONFIG_PATH", root / "config.toml"), ("STATE_PATH", root / "state.json")):
        p = patch.object(cfg, name, value)
        p.start()
        case.addCleanup(p.stop)
    original = cfg.CONFIG
    case.addCleanup(setattr, cfg, "CONFIG", original)
    return root


def use_config(case, **changes) -> None:
    """Replace fields on CONFIG for this test."""
    original = cfg.CONFIG
    case.addCleanup(setattr, cfg, "CONFIG", original)
    cfg.CONFIG = dataclasses.replace(original, **changes)


class FakeApp:
    """Stands in for the rumps app: the code only keeps _rg_* state on it."""
