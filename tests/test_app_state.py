"""App-object state: notices, menu toggles, and the hot-reload contract."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import logic
import rg
from rg import app as rg_app
from rg import config as cfg
from rg import menu, notices, rows

from .support import FakeApp, isolate_config


class NoticeTests(unittest.TestCase):
    def test_add_dedupe_cap_and_dismiss(self):
        app = FakeApp()
        app._rg_snapshot = object()
        notices.add_notice(app, "a")
        notices.add_notice(app, "a")
        self.assertEqual(notices.notices(app), ["a"])
        self.assertTrue(app._rg_pending)
        for i in range(10):
            notices.add_notice(app, str(i))
        self.assertEqual(len(notices.notices(app)), notices.MAX_NOTICES)
        notices.dismiss_notice(app, "9")
        self.assertNotIn("9", notices.notices(app))

    def test_no_rerender_flag_before_first_snapshot(self):
        app = FakeApp()
        notices.add_notice(app, "a")
        self.assertFalse(getattr(app, "_rg_pending", False))


class ToggleTests(unittest.TestCase):
    def setUp(self):
        isolate_config(self)
        cfg.CONFIG_PATH.write_text('title = "WIP"\n')
        cfg.CONFIG = cfg.load_config()

    def test_mark_important_round_trip(self):
        app = FakeApp()
        rows._on_toggle_important(app, "/r/a", None)
        cfg.CONFIG = cfg.load_config()
        self.assertEqual(cfg.CONFIG.important, frozenset({"a"}))
        rows._on_toggle_important(app, "/r/a", None)
        cfg.CONFIG = cfg.load_config()
        self.assertEqual(cfg.CONFIG.important, frozenset())
        self.assertEqual(notices.notices(app), [])
        self.assertNotIn("important", cfg.CONFIG_PATH.read_text())  # config untouched

    def test_refused_toggle_becomes_a_notice(self):
        cfg.STATE_PATH.write_text("{broken")
        cfg.CONFIG = cfg.load_config()
        app = FakeApp()
        rows._on_toggle_important(app, "/r/a", None)
        self.assertEqual(len(notices.notices(app)), 1)
        self.assertEqual(cfg.STATE_PATH.read_text(), "{broken")


class ReloadContractTests(unittest.TestCase):
    def test_modules_only_import_earlier_modules(self):
        """logic.py reloads rg.MODULES in order; a module importing a later one
        would bind that module's previous version on every reload."""
        order = {name: i for i, name in enumerate(rg.MODULES)}
        package = Path(rg.__file__).parent
        self.assertEqual(
            sorted(p.stem for p in package.glob("*.py") if p.stem != "__init__"),
            sorted(rg.MODULES),
        )
        for name in rg.MODULES:
            tree = ast.parse((package / f"{name}.py").read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    deps = [node.module] if node.module else [a.name for a in node.names]
                    for dep in deps:
                        self.assertLess(order[dep], order[name], f"rg.{name} imports rg.{dep}")

    def test_syntax_error_is_caught_before_anything_reloads(self):
        with tempfile.TemporaryDirectory() as temp:
            good, bad = Path(temp, "good.py"), Path(temp, "bad.py")
            good.write_text("x = 1\n")
            bad.write_text("def broken(:\n")
            logic._check_sources([good])
            with self.assertRaises(SyntaxError):
                logic._check_sources([good, bad])

    def test_only_changed_modules_and_their_successors_reload(self):
        modules = ("a", "b", "c", "d")
        mtimes = {"a": 1, "b": 1, "c": 1, "d": 1}
        everything = set(modules)
        self.assertEqual(logic._to_reload(modules, mtimes, dict(mtimes), everything), [])
        self.assertEqual(logic._to_reload(modules, mtimes, {**mtimes, "c": 0}, everything), ["c", "d"])
        self.assertEqual(logic._to_reload(modules, mtimes, {}, everything), list(modules))
        self.assertEqual(logic._to_reload(modules, mtimes, dict(mtimes), {"a", "c", "d"}), ["b", "c", "d"])

    def test_loaded_mtimes_survive_reloading_the_facade(self):
        import importlib
        before = logic._LOADED
        importlib.reload(logic)
        self.assertIs(logic._LOADED, before)
        self.assertEqual(rg.last_reload[2], 0)  # nothing changed on disk

    def test_facade_exports_the_launcher_entry_points(self):
        for name in ("tick", "refresh", "cli_tick", "TICK_SECONDS", "_row_draw", "_row_event"):
            self.assertTrue(hasattr(logic, name), name)


class SlowOpenLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = Path(temp.name) / "repo-glance.log"
        p = patch.object(menu, "LOG_PATH", self.log)
        p.start()
        self.addCleanup(p.stop)

    def open_after(self, seconds, event_type=menu.NSEventTypeLeftMouseDown):
        event = MagicMock()
        event.type.return_value = event_type
        event.timestamp.return_value = 100.0
        info = MagicMock()
        info.systemUptime.return_value = 100.0 + seconds
        app = FakeApp()
        nsapp = MagicMock()
        nsapp.sharedApplication.return_value.currentEvent.return_value = event
        process = MagicMock()
        process.processInfo.return_value = info
        # Patch the module's names, never the real ObjC classes.
        with patch.object(menu, "NSApplication", nsapp), patch.object(menu, "NSProcessInfo", process):
            menu._on_menu_will_open(app)
        return app

    def test_fast_open_logs_nothing(self):
        app = self.open_after(0.05)
        self.assertFalse(self.log.exists())
        self.assertEqual(app._rg_menu_opens, 1)

    def test_slow_open_logs_latency_and_context(self):
        self.open_after(0.4)
        line = self.log.read_text()
        self.assertIn("menu opened 400 ms after the click", line)
        self.assertIn("App Nap allowed", line)

    def test_non_mouse_events_are_ignored(self):
        self.open_after(5, event_type=10)  # key down
        self.assertFalse(self.log.exists())

    def test_log_rotates_when_large(self):
        self.log.write_text("x" * (cfg.LOG_MAX_BYTES + 1))
        self.open_after(1)
        self.assertTrue(self.log.with_name("repo-glance.log.old").exists())
        self.assertIn("menu opened", self.log.read_text())


class KeepAwakeTests(unittest.TestCase):
    def test_activity_is_taken_once(self):
        app = FakeApp()
        info = MagicMock()
        process = MagicMock()
        process.processInfo.return_value = info
        with patch.object(rg_app, "NSProcessInfo", process):
            rg_app._keep_awake(app)
            rg_app._keep_awake(app)
        info.beginActivityWithOptions_reason_.assert_called_once()
        self.assertIsNotNone(app._rg_activity)


if __name__ == "__main__":
    unittest.main()
