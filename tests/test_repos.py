"""Repo discovery, sorting, fingerprints, and the merged-PR cleanup action."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rg import config as cfg
from rg import repos

from .support import use_config


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
         *args], cwd=cwd, capture_output=True, text=True, env=env, check=True,
    )
    return result.stdout.strip()


class SortAndFilterTests(unittest.TestCase):
    def test_natural_key_orders_numbers_numerically(self):
        names = ["playmaker10", "playmaker", "playmaker9", "playmaker2", "playwright"]
        self.assertEqual(
            sorted(names, key=repos._natural_key),
            ["playmaker", "playmaker2", "playmaker9", "playmaker10", "playwright"],
        )

    def test_shown_applies_repo_and_hide_patterns(self):
        use_config(
            self,
            repo_pattern=cfg._full_match_pattern(r"playmaker\d*"),
            hide_pattern=cfg._full_match_pattern(["playmaker[0-3]"]),
        )
        self.assertTrue(repos._shown("playmaker"))
        self.assertTrue(repos._shown("playmaker10"))
        self.assertFalse(repos._shown("playmaker2"))
        self.assertFalse(repos._shown("rebound"))

    def test_candidate_repos_scans_dirs_in_natural_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("p10", "p9", "p1", "other"):
                (root / name).mkdir()
            (root / "p2").write_text("a file, not a dir")
            use_config(self, scan_dirs=[root, root / "missing"],
                       repo_pattern=cfg._full_match_pattern(r"p\d+"), hide_pattern=None)
            self.assertEqual(
                [Path(r).name for r in repos._candidate_repos()], ["p1", "p9", "p10"]
            )


class ChangedDuringTests(unittest.TestCase):
    def fingerprint(self, *, code=1, head=1, index=1):
        header = [code] * len(repos._code_files()) + [1, 1]
        return tuple(header + ["/r", head, index, 1, 1])

    def test_index_only_change_is_ignored(self):
        self.assertFalse(repos._changed_during(self.fingerprint(), self.fingerprint(index=2)))

    def test_head_or_code_change_counts(self):
        self.assertTrue(repos._changed_during(self.fingerprint(), self.fingerprint(head=2)))
        self.assertTrue(repos._changed_during(self.fingerprint(), self.fingerprint(code=2)))

    def test_repo_set_change_counts(self):
        self.assertTrue(repos._changed_during(self.fingerprint(), self.fingerprint()[:-5]))


class MergedCleanupTests(unittest.TestCase):
    """switch_to_default_and_pull against a real origin and two clones."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rg-git-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.origin = root / "origin.git"
        git(root, "init", "--bare", str(self.origin))
        self.repo = root / "checkout"
        git(root, "clone", str(self.origin), str(self.repo))
        (self.repo / "f").write_text("1")
        git(self.repo, "add", "f")
        git(self.repo, "commit", "-m", "one")
        git(self.repo, "push", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "--auto")
        git(self.repo, "switch", "-c", "feature")
        # Someone else lands a commit on main.
        other = root / "other"
        git(root, "clone", str(self.origin), str(other))
        (other / "g").write_text("2")
        git(other, "add", "g")
        git(other, "commit", "-m", "two")
        git(other, "push", "origin", "main")

    def test_default_branch_comes_from_origin_head(self):
        self.assertEqual(repos.default_branch(str(self.repo)), "main")

    def test_switches_and_fast_forwards(self):
        self.assertIsNone(repos.switch_to_default_and_pull(str(self.repo)))
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")
        self.assertTrue((self.repo / "g").exists())

    def test_refuses_with_uncommitted_changes(self):
        (self.repo / "f").write_text("dirty")
        error = repos.switch_to_default_and_pull(str(self.repo))
        self.assertIn("uncommitted", error)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "feature")

    def test_reports_git_failures(self):
        git(self.repo, "remote", "set-url", "origin", str(self.origin) + "-gone")
        error = repos.switch_to_default_and_pull(str(self.repo))
        self.assertTrue(error.startswith("git pull failed"), error)


if __name__ == "__main__":
    unittest.main()
