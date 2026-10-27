"""Text output: row formatting/markers, WIP.md, and WIP.json."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from rg import fmt, wip
from rg.cmux import AgentState
from rg.repos import RepoInfo
from rg.snapshot import Snapshot

from .support import isolate_config, use_config


def info(name, branch="feat/GO-1-x", dirty=0, is_git=True):
    return RepoInfo(repo=f"/r/{name}", branch=branch, epoch=0, last_iso="2026-09-30",
                    uncommitted=dirty, is_git=is_git)


PR = {"number": 7, "title": "fix | pipes", "url": "https://x/7", "state": "OPEN",
      "isDraft": False, "reviewDecision": "APPROVED",
      "statusCheckRollup": [{"conclusion": "FAILURE"}]}


READING = (12, datetime(2026, 10, 1, 14, 50).astimezone(), datetime(2026, 10, 1, 9, 0).astimezone(), False)


def snapshot(infos, prs=None, usage=None):
    return Snapshot(infos=infos, prs=prs or {}, usage=[("Claude", READING)] if usage is None else usage,
                    codex=None, sync=None, app_behind=0, fingerprint=(),
                    collected_at=datetime(2026, 10, 1, 9, 0), changed_during=False)


class GutterTests(unittest.TestCase):
    def test_no_columns_when_nothing_is_marked(self):
        use_config(self, important=frozenset())
        self.assertEqual(fmt._gutter("/r/a", {}), "")

    def test_each_column_appears_only_when_used(self):
        use_config(self, important=frozenset({"b"}))
        agents = {"/r/a": AgentState("waiting", "w")}
        self.assertEqual(fmt._gutter("/r/a", agents), "✳   ")
        self.assertEqual(fmt._gutter("/r/b", agents), "  ★ ")
        self.assertEqual(fmt._gutter("/r/c", agents), "    ")
        self.assertEqual(fmt._gutter("/r/b", {}), "★ ")

    def test_columns_stay_aligned_with_markers(self):
        use_config(self, important=frozenset({"a"}), branch_width=10)
        agents = {"/r/bb": AgentState("running", "w")}
        rows = [fmt.repo_summary(info(n), 5, agents) for n in ("a", "bb", "ccc")]
        self.assertEqual({row.index("feat/") for row in rows}, {rows[0].index("feat/")})
        self.assertTrue(rows[1].startswith("◐"))


class WipTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate_config(self)
        use_config(self, important=frozenset({"a"}), show_pr=True, show_usage=True,
                   wip_file=str(self.root / "WIP.md"))
        self.snap = snapshot([info("a"), info("b", dirty=3), info("c", branch="", is_git=False)],
                             prs={"/r/b": PR})
        self.agents = {"/r/b": AgentState("waiting", "w", "n")}

    def test_markdown_rows_markers_and_escaping(self):
        text = wip._wip_markdown(self.snap, self.agents)
        self.assertIn("| ★ a |", text)
        self.assertIn("| ✳ b |", text)
        self.assertIn("[#7 fix \\| pipes](https://x/7) (CI failed, Approved)", text)
        self.assertIn("| c | /r/c | (not a git repo) |", text)
        self.assertIn("- Claude (5-hour session): 12% used", text)

    def test_json_has_the_same_facts(self):
        data = wip._wip_data(self.snap, self.agents)
        self.assertEqual(data["version"], wip.WIP_JSON_VERSION)
        by_name = {r["name"]: r for r in data["repos"]}
        self.assertTrue(by_name["a"]["important"])
        self.assertEqual(by_name["b"]["agent"], "waiting")
        self.assertEqual(by_name["b"]["pr"]["ci"], "failed")
        self.assertEqual(by_name["b"]["pr"]["title"], "fix | pipes")
        self.assertEqual(by_name["b"]["ticket"], "GO-1")
        self.assertIsNone(by_name["c"]["branch"])
        self.assertEqual(data["usage"]["claude"]["percent_used"], 12)
        self.assertEqual(data["usage"]["claude_accounts"][0]["label"], "Claude")
        json.dumps(data)  # serialisable

    def test_every_claude_account_is_listed(self):
        snap = snapshot([info("a")], usage=[("Work", READING), ("Home", None)])
        text = wip._wip_markdown(snap, {})
        self.assertIn("- Work (5-hour session): 12% used", text)
        self.assertIn("- Home: usage unavailable", text)
        data = wip._wip_data(snap, {})
        self.assertEqual(data["usage"]["claude"]["percent_used"], 12)
        self.assertEqual([a["label"] for a in data["usage"]["claude_accounts"]], ["Work", "Home"])
        self.assertIsNone(data["usage"]["claude_accounts"][1]["usage"])

    def test_codex_label_only_changes_the_menu(self):
        use_config(self, codex_label="GPT")
        snap = snapshot([info("a")], usage=[("A", READING)])
        snap.codex = READING
        self.assertIn("- Codex: 12% used", wip._wip_markdown(snap, {}))
        self.assertIn("codex", wip._wip_data(snap, {})["usage"])

    def test_old_single_reading_snapshot_still_renders(self):
        snap = snapshot([info("a")], usage=READING)  # shape from before claude_dirs
        self.assertIn("- Claude (5-hour session): 12% used", wip._wip_markdown(snap, {}))

    def test_write_produces_both_files_atomically(self):
        wip._write_wip(self.snap, self.agents)
        self.assertIn("# WIP", (self.root / "WIP.md").read_text())
        self.assertEqual(json.loads((self.root / "WIP.json").read_text())["version"], 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.endswith(".tmp")), [])

    def test_empty_wip_file_disables_writing(self):
        use_config(self, wip_file="")
        wip._write_wip(self.snap, self.agents)
        self.assertFalse((self.root / "WIP.md").exists())


if __name__ == "__main__":
    unittest.main()
