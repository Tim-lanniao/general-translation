"""Persistent cache for finished text translations.

Entries are keyed on the complete upstream request (model, messages with the matched
glossary lines, sampling options), so a changed term only invalidates the sentences
that contain it.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class TranslationCache:
    def __init__(self, path: Path, max_entries: int = 20000):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.max_entries = max(100, int(max_entries))
        self.lock = threading.Lock()
        self.writes = 0
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, content TEXT NOT NULL, model TEXT,"
                        " created REAL NOT NULL, used REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE INDEX IF NOT EXISTS entries_used ON entries (used)")

    def get(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT content FROM entries WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE entries SET used = ?, hits = hits + 1 WHERE key = ?", (time.time(), key))
            return row[0]

    def put(self, key: str, content: str, model: str) -> None:
        now = time.time()
        with self.lock:
            self.db.execute("INSERT INTO entries (key, content, model, created, used) VALUES (?, ?, ?, ?, ?)"
                            " ON CONFLICT(key) DO UPDATE SET content = excluded.content, used = excluded.used",
                            (key, content, model, now, now))
            self.writes += 1
            if self.writes % 200 == 0:
                self._prune()

    def _prune(self) -> None:
        excess = self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] - self.max_entries
        if excess > 0:
            self.db.execute("DELETE FROM entries WHERE key IN (SELECT key FROM entries ORDER BY used LIMIT ?)", (excess,))

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def clear(self) -> int:
        with self.lock:
            return self.db.execute("DELETE FROM entries").rowcount

    def close(self) -> None:
        with self.lock:
            self.db.close()
