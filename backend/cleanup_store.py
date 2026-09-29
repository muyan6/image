"""Durable, idempotent media expiry/deletion queue (including COS objects)."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

from cos_store import delete_object

log = logging.getLogger("rescue.cleanup")


class CleanupStore:
    def __init__(self, data_dir: str, upload_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._root = os.path.realpath(upload_dir)
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._conn = sqlite3.connect(os.path.join(data_dir, "cleanup.db"), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS cleanup (kind TEXT, target TEXT, "
                           "due REAL NOT NULL, next_try REAL NOT NULL, attempts INTEGER DEFAULT 0, "
                           "PRIMARY KEY(kind,target))")
        self._conn.commit()

    def schedule(self, kind: str, target: str, due: float) -> None:
        if not target:
            return
        if kind not in ("local", "cos"):
            raise ValueError("unknown cleanup kind")
        if kind == "local":
            path = os.path.realpath(os.path.join(self._root, target))
            if os.path.commonpath([path, self._root]) != self._root or os.path.basename(target) != target:
                raise ValueError("invalid cleanup path")
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO cleanup(kind,target,due,next_try) VALUES(?,?,?,?) "
                               "ON CONFLICT(kind,target) DO UPDATE SET "
                               "due=MIN(due,excluded.due),next_try=MIN(next_try,excluded.next_try)",
                               (kind, target, due, due))

    def run(self, settings, limit: int = 100) -> int:
        if not self._run_lock.acquire(blocking=False):
            return 0
        removed = 0
        try:
            now = time.time()
            with self._lock:
                rows = self._conn.execute("SELECT kind,target,attempts FROM cleanup "
                                          "WHERE due<=? AND next_try<=? ORDER BY due LIMIT ?",
                                          (now, now, limit)).fetchall()
            for kind, target, attempts in rows:
                try:
                    if kind == "local":
                        path = os.path.realpath(os.path.join(self._root, target))
                        if os.path.commonpath([path, self._root]) != self._root:
                            raise ValueError("invalid cleanup path")
                        try:
                            os.remove(path)
                        except FileNotFoundError:
                            pass
                    else:
                        if not settings.cos_ready():
                            raise RuntimeError("COS configuration pending")
                        delete_object(settings, target)
                except Exception:
                    log.warning("媒体清理将在后台重试 kind=%s", kind)
                    with self._lock, self._conn:
                        self._conn.execute("UPDATE cleanup SET attempts=attempts+1,next_try=? "
                                           "WHERE kind=? AND target=?",
                                           (now + min(3600, 30 * 2 ** min(attempts, 7)), kind, target))
                else:
                    with self._lock, self._conn:
                        self._conn.execute("DELETE FROM cleanup WHERE kind=? AND target=?", (kind, target))
                    removed += 1
            return removed
        finally:
            self._run_lock.release()

    def close(self) -> None:
        self._conn.close()
