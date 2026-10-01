"""Codex navigation checks; run with python -m unittest test_codex."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import logic


class CodexNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex # ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = str(self.home / "repo & space")
        env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def database(self, version=5, milliseconds=False):
        path = self.home / f"state_{version}.sqlite"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT, cwd TEXT, updated_at INTEGER, "
                "archived INTEGER, source TEXT)"
            )
            if milliseconds:
                connection.execute("ALTER TABLE threads ADD updated_at_ms INTEGER")
        return path

    def insert(self, path, rows):
        with sqlite3.connect(path) as connection:
            connection.executemany(
                "INSERT INTO threads (id, cwd, updated_at, archived, source) "
                "VALUES (?, ?, ?, ?, ?)", rows,
            )

    def opened_url(self):
        with patch.object(logic.subprocess, "run") as run:
            run.return_value.returncode = 0
            logic._open_codex_repo(self.repo)
        self.assertEqual(run.call_args.args[0][0], "open")
        return run.call_args.args[0][1]

    def assert_new_chat(self):
        url = urlparse(self.opened_url())
        self.assertEqual((url.scheme, url.netloc), ("codex", "new"))
        self.assertEqual(parse_qs(url.query), {"path": [str(Path(self.repo).resolve())]})

    def test_latest_chat_excludes_archived_helpers_and_other_checkouts(self):
        path = self.database()
        self.insert(path, [
            ("older", self.repo, 10, 0, "cli"),
            ("latest", self.repo, 20, 0, "vscode"),
            ("archived", self.repo, 30, 1, "vscode"),
            ("helper", self.repo, 40, 0, '{"subagent":{"other":"guardian"}}'),
            ("exec", self.repo, 50, 0, "exec"),
            ("other", self.repo + "-worktree", 60, 0, "vscode"),
        ])
        self.assertEqual(self.opened_url(), "codex://threads/latest")

    def test_millisecond_recency_breaks_second_ties(self):
        path = self.database(milliseconds=True)
        self.insert(path, [(name, self.repo, 20, 0, "vscode") for name in ("a", "z")])
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE threads SET updated_at_ms = 20900 WHERE id = 'a'")
        self.assertEqual(logic._latest_codex_thread(self.repo), "a")

    def test_missing_index_opens_new_chat_without_creating_database(self):
        self.assert_new_chat()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_no_matching_chat_opens_new_chat(self):
        self.insert(self.database(), [("other", self.repo + "-other", 20, 0, "vscode")])
        self.assert_new_chat()

    def test_newest_schema_is_selected_numerically(self):
        self.insert(self.database(9), [("stale", self.repo, 99, 0, "vscode")])
        self.insert(self.database(10), [("current", self.repo, 20, 0, "vscode")])
        self.assertEqual(logic._latest_codex_thread(self.repo), "current")

    def test_corrupt_or_incompatible_index_falls_back_without_using_stale_chats(self):
        self.insert(self.database(4), [("stale", self.repo, 99, 0, "vscode")])
        path = self.home / "state_5.sqlite"
        path.write_bytes(b"not a sqlite database")
        self.assert_new_chat()
        self.assertEqual(path.read_bytes(), b"not a sqlite database")
        path.unlink()
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE threads (id TEXT)")
        self.assert_new_chat()


if __name__ == "__main__":
    unittest.main()
