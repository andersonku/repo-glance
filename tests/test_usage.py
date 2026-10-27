"""Claude usage: per-config-dir keychain items, labels, and the menu rows."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rg import config as cfg
from rg import usage
from rg.snapshot import Snapshot, claude_usages


class KeychainServiceTests(unittest.TestCase):
    def test_default_dir_uses_the_plain_service_name(self):
        for d in ("~/.claude", os.path.expanduser("~/.claude"), "~/.claude/"):
            self.assertEqual(cfg.keychain_service(d), "Claude Code-credentials")

    def test_other_dirs_get_a_sha256_suffix_of_the_absolute_path(self):
        path = os.path.expanduser("~/.claude-ak")
        digest = hashlib.sha256(path.encode()).hexdigest()[:8]
        self.assertEqual(cfg.keychain_service("~/.claude-ak"), f"Claude Code-credentials-{digest}")
        self.assertEqual(cfg.keychain_service("~/.claude-ak/"), f"Claude Code-credentials-{digest}")

    def test_labels_come_from_the_dir_name(self):
        self.assertEqual(cfg.claude_label("~/.claude"), "Claude")
        self.assertEqual(cfg.claude_label("~/.claude-ak"), "Claude ak")
        self.assertEqual(cfg.claude_label("~/.claude_work/"), "Claude work")
        self.assertEqual(cfg.claude_label("/opt/cfg/.personal"), "personal")


class OauthTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rg-usage-")
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / ".claude-x"
        self.dir.mkdir()

    def fake_security(self, stdout="", returncode=1):
        return patch.object(usage.subprocess, "run",
                            return_value=SimpleNamespace(stdout=stdout, returncode=returncode))

    def credentials(self, token="tok", expires_in=3600):
        expires = (datetime.now() + timedelta(seconds=expires_in)).timestamp() * 1000
        return json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": expires}})

    def test_keychain_item_for_that_dir_is_asked_for(self):
        with self.fake_security(self.credentials("kc"), 0) as run:
            self.assertEqual(usage._oauth_token(str(self.dir)), "kc")
        self.assertEqual(run.call_args.args[0][3], cfg.keychain_service(str(self.dir)))

    def test_falls_back_to_credentials_file_in_the_dir(self):
        (self.dir / ".credentials.json").write_text(self.credentials("file"))
        with self.fake_security():
            self.assertEqual(usage._oauth_token(str(self.dir)), "file")

    def test_expired_or_missing_token_is_none(self):
        with self.fake_security(self.credentials("old", -5), 0):
            self.assertIsNone(usage._oauth_token(str(self.dir)))
        with self.fake_security():
            self.assertIsNone(usage._oauth_token(str(self.dir)))


class UsageStateTests(unittest.TestCase):
    def test_each_dir_is_polled_and_cached_separately(self):
        app = SimpleNamespace()
        soon = datetime.now().astimezone() + timedelta(hours=1)
        lookups = {"~/.claude": (10, soon), "~/.claude-ak": (70, soon)}
        with patch.object(usage, "_usage_lookup", side_effect=lambda d: lookups[d]) as lookup:
            a = usage._usage(app, "~/.claude")
            b = usage._usage(app, "~/.claude-ak")
            usage._usage(app, "~/.claude")  # within USAGE_SECONDS: cached
        self.assertEqual((a[0], b[0], a[3]), (10, 70, False))
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(len(app._rg_usage_by_dir), 2)


class SnapshotCompatTests(unittest.TestCase):
    def test_old_single_reading_shape_is_wrapped(self):
        snap = Snapshot(infos=[], prs={}, usage=(1, None, None, False), codex=None, sync=None,
                        app_behind=0, fingerprint=(), collected_at=datetime.now(), changed_during=False)
        self.assertEqual(claude_usages(snap), [("Claude", (1, None, None, False))])
        snap.usage = None
        self.assertEqual(claude_usages(snap), [("Claude", None)])


if __name__ == "__main__":
    unittest.main()
