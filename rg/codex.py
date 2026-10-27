"""Open a checkout's latest Codex chat (or a new one) via codex:// URLs."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from contextlib import closing
from pathlib import Path

def _latest_codex_thread(repo: str) -> str | None:
    """Read the latest user chat for this checkout from Codex's local index."""
    codex_home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    try:
        databases = [
            path
            for path in codex_home.glob("state_*.sqlite")
            if re.fullmatch(r"state_\d+\.sqlite", path.name)
        ]
        if not databases:
            return None
        # Only use the newest schema; older databases may contain stale chats.
        database = max(databases, key=lambda path: int(path.stem.split("_")[1]))
        checkout = Path(repo).expanduser().absolute()
        with closing(
            sqlite3.connect(
                database.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2
            )
        ) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)")
            }
            updated = (
                "COALESCE(updated_at_ms, updated_at * 1000)"
                if "updated_at_ms" in columns else "updated_at"
            )
            row = connection.execute(
                f"SELECT id FROM threads WHERE archived = 0 AND cwd IN (?, ?) "
                "AND source NOT LIKE '%subagent%' AND source != 'exec' "
                f"ORDER BY {updated} DESC, id DESC LIMIT 1",
                (str(checkout), str(checkout.resolve())),
            ).fetchone()
            return row[0] if row else None
    except (OSError, sqlite3.Error):
        # The index is an app internal: missing/changed schemas must not break
        # opening a repo, and reading must never create or migrate a database.
        return None


def _open_codex_repo(repo: str) -> None:
    thread_id = _latest_codex_thread(repo)
    if thread_id:
        url = "codex://threads/" + urllib.parse.quote(thread_id, safe="")
    else:
        url = "codex://new?" + urllib.parse.urlencode(
            {"path": str(Path(repo).expanduser().resolve())}
        )
    try:
        result = subprocess.run(
            ["open", url], capture_output=True, text=True, timeout=5
        )
        if result.returncode:
            print(
                f"repo-glance: could not open Codex: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"repo-glance: could not open Codex: {error}", file=sys.stderr)


def open_in_codex(repo: str) -> None:
    """Resolve the chat and launch Codex without blocking the menu's UI thread."""
    threading.Thread(target=_open_codex_repo, args=(repo,), daemon=True).start()
