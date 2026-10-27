"""config.toml parsing, error collection, and state.json."""

from __future__ import annotations

import json
import re
import unittest

from rg import config as cfg

from .support import isolate_config


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate_config(self)

    def write(self, text: str) -> None:
        cfg.CONFIG_PATH.write_text(text)

    def test_valid_file_is_used_and_has_no_errors(self):
        self.write('title = "WIP"\nrefresh_seconds = 30\nrepo_pattern = ["a\\\\d*", "b"]\n')
        config = cfg.load_config()
        self.assertEqual((config.title, config.refresh_seconds), ("WIP", 30))
        self.assertTrue(config.repo_pattern.match("a12"))
        self.assertFalse(config.repo_pattern.match("a12x"))
        self.assertEqual(config.errors, ())
        self.assertFalse(config.fallback)

    def test_duplicate_key_falls_back_to_defaults_with_an_error(self):
        self.write('title = "WIP"\nhide_pattern = []\nhide_pattern = ["x"]\n')
        config = cfg.load_config()
        self.assertTrue(config.fallback)
        self.assertEqual(config.title, cfg.DEFAULTS["title"])
        (path, message), = config.errors
        self.assertEqual(path, cfg.CONFIG_PATH)
        self.assertIn("Cannot overwrite a value", message)

    def test_bad_value_only_resets_that_key(self):
        self.write('title = "WIP"\nrepo_pattern = "("\nrefresh_seconds = "60"\n')
        config = cfg.load_config()
        self.assertEqual(config.title, "WIP")
        self.assertEqual(config.repo_pattern.pattern, cfg._full_match_pattern(cfg.DEFAULTS["repo_pattern"]).pattern)
        self.assertEqual(config.refresh_seconds, cfg.DEFAULTS["refresh_seconds"])
        keys = sorted(m.split(":")[1].strip() for _, m in config.errors)
        self.assertEqual(keys, ["refresh_seconds", "repo_pattern"])
        self.assertFalse(config.fallback)

    def test_claude_dirs_list_and_table(self):
        self.write('claude_dirs = ["~/.claude", "~/.claude-ak"]\n')
        config = cfg.load_config()
        self.assertEqual([label for label, _ in config.claude_dirs], ["Claude", "Claude ak"])
        self.assertTrue(config.claude_dirs[1][1].endswith("/.claude-ak"))
        self.assertFalse(config.claude_dirs[1][1].startswith("~"))
        self.write('claude_dirs = { Work = "~/.claude", Home = "~/.claude-ak" }\n')
        config = cfg.load_config()
        self.assertEqual([label for label, _ in config.claude_dirs], ["Work", "Home"])
        self.assertEqual(config.errors, ())

    def test_empty_or_wrong_claude_dirs_fall_back(self):
        self.write('claude_dirs = []\n')
        config = cfg.load_config()
        self.assertEqual(config.claude_dirs, cfg._claude_dirs(cfg.DEFAULTS["claude_dirs"]))
        self.assertIn("claude_dirs", config.errors[0][1])

    def test_bad_sort_value_is_reported(self):
        self.write('sort = "random"\n')
        config = cfg.load_config()
        self.assertEqual(config.sort, "name")
        self.assertIn("sort", config.errors[0][1])

    def test_missing_file_is_created_from_the_sample(self):
        cfg.load_config()
        self.assertEqual(cfg.CONFIG_PATH.read_text(), cfg.SAMPLE_CONFIG)

    def test_missing_keys_are_appended_as_commented_defaults(self):
        self.write('title = "WIP"\n')
        cfg.load_config()
        text = cfg.CONFIG_PATH.read_text()
        self.assertIn("wip_file = ", text)
        self.assertIn(cfg.KEY_COMMENTS["wip_file"], text)
        self.assertEqual(text.count('title = "WIP"'), 1)

    def test_ui_state_keys_are_not_appended_to_config(self):
        self.write('title = "WIP"\n')
        cfg.load_config()
        text = cfg.CONFIG_PATH.read_text()
        self.assertNotIn("important =", text)
        self.assertNotIn("show_pr =", text)


class FullMatchPatternTests(unittest.TestCase):
    def test_list_is_anchored_alternation(self):
        pattern = cfg._full_match_pattern(["playmaker[0-3]", "rebound"])
        self.assertTrue(pattern.match("playmaker2"))
        self.assertTrue(pattern.match("rebound"))
        self.assertFalse(pattern.match("playmaker10"))
        self.assertFalse(pattern.match("rebound2"))

    def test_empty_means_none(self):
        self.assertIsNone(cfg._full_match_pattern([]))
        self.assertIsNone(cfg._full_match_pattern(""))
        self.assertIsNone(cfg._full_match_pattern(["", ""]))

    def test_invalid_input_raises(self):
        with self.assertRaises(re.error):
            cfg._full_match_pattern("(")
        with self.assertRaises(TypeError):
            cfg._full_match_pattern(3)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate_config(self)
        cfg.CONFIG_PATH.write_text('title = "WIP"\n')

    def reload(self):
        cfg.CONFIG = cfg.load_config()
        return cfg.CONFIG

    def test_defaults_without_state_file(self):
        config = self.reload()
        self.assertEqual((config.important, config.show_pr), (frozenset(), True))

    def test_legacy_config_values_seed_state(self):
        cfg.CONFIG_PATH.write_text('important = ["a", "b"]\nshow_pr = false\n')
        config = self.reload()
        self.assertEqual((config.important, config.show_pr), (frozenset({"a", "b"}), False))

    def test_state_file_wins_over_legacy_config(self):
        cfg.CONFIG_PATH.write_text('important = ["a"]\n')
        cfg.STATE_PATH.write_text(json.dumps({"important": ["z"]}))
        self.assertEqual(self.reload().important, frozenset({"z"}))

    def test_save_state_merges_and_persists(self):
        self.reload()
        self.assertIsNone(cfg.save_state(important=["x"]))
        self.reload()
        self.assertIsNone(cfg.save_state(show_pr=False))
        self.assertEqual(json.loads(cfg.STATE_PATH.read_text()), {"important": ["x"], "show_pr": False})
        config = self.reload()
        self.assertEqual((config.important, config.show_pr), (frozenset({"x"}), False))
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_corrupt_state_is_reported_and_never_overwritten(self):
        cfg.STATE_PATH.write_text("{not json")
        config = self.reload()
        self.assertFalse(config.state_ok)
        self.assertEqual(config.errors[0][0], cfg.STATE_PATH)
        self.assertIsNotNone(cfg.save_state(important=["x"]))
        self.assertEqual(cfg.STATE_PATH.read_text(), "{not json")

    def test_wrong_state_type_is_reported(self):
        cfg.STATE_PATH.write_text(json.dumps({"important": "a"}))
        config = self.reload()
        self.assertEqual(config.important, frozenset())
        self.assertIn("important", config.errors[0][1])

    def test_no_seeding_write_while_config_is_unparseable(self):
        # Legacy values can't be read, so writing would drop them.
        cfg.CONFIG_PATH.write_text('important = ["a"]\nimportant = ["b"]\n')
        self.reload()
        self.assertIsNotNone(cfg.save_state(important=["x"]))
        self.assertFalse(cfg.STATE_PATH.exists())

    def test_existing_state_is_writable_even_if_config_is_broken(self):
        cfg.STATE_PATH.write_text(json.dumps({"important": ["a"]}))
        cfg.CONFIG_PATH.write_text("title = \n")
        self.reload()
        self.assertIsNone(cfg.save_state(important=["a", "b"]))
        self.assertEqual(json.loads(cfg.STATE_PATH.read_text())["important"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
