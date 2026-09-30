"""Durable, idempotent media expiry/deletion queue (including COS objects)."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        self._conn.execute('CREATE TABLE IF NOT EXISTS upload_cleanup_guard(target TEXT PRIMARY KEY,expires REAL NOT NULL)')
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
                        self._deleted_locked(kind,target,time.time())
                    removed += 1
            return removed
        finally:
            self._run_lock.release()

    def delete_cos_now(self, settings, keys) -> int:
        """用户删作品时只立即处理该作品 COS 对象；失败项保留在持久队列重试。"""
        targets = list(dict.fromkeys(k for k in keys if k))
        if not targets or not settings.cos_ready():
            return 0
        removed = 0
        with self._run_lock:
            with ThreadPoolExecutor(max_workers=min(3, len(targets))) as pool:
                future_keys = {pool.submit(delete_object, settings, key, timeout=(3, 5)): key
                               for key in targets}
                for future in as_completed(future_keys):
                    key = future_keys[future]
                    try:
                        future.result()
                    except Exception:
                        log.warning("COS 立即删除未完成，后台队列继续重试 key=%s", key)
                    else:
                        with self._lock, self._conn:
                            self._deleted_locked('cos',key,time.time())
                        removed += 1
        return removed

    def protect_upload_until(self,target,expires):
        """Delete once more after a still-valid PUT URL can no longer be reused."""
        if not target.startswith('uploads/'):raise ValueError('Upload guard requires uploads/ prefix')
        with self._lock,self._conn:
            self._conn.execute('INSERT INTO upload_cleanup_guard VALUES(?,?) ON CONFLICT(target) DO UPDATE SET expires=MAX(expires,excluded.expires)',(target,expires+300))

    def _deleted_locked(self,kind,target,now):
        guard=self._conn.execute('SELECT expires FROM upload_cleanup_guard WHERE target=?',(target,)).fetchone() if kind=='cos' else None
        if guard and guard[0]>now:
            self._conn.execute("INSERT INTO cleanup(kind,target,due,next_try) VALUES('cos',?,?,?) ON CONFLICT(kind,target) DO UPDATE SET due=excluded.due,next_try=excluded.next_try,attempts=0",(target,guard[0],guard[0]))
        else:
            self._conn.execute('DELETE FROM cleanup WHERE kind=? AND target=?',(kind,target))
            if kind=='cos':self._conn.execute('DELETE FROM upload_cleanup_guard WHERE target=?',(target,))

    def retain_until(self, kind: str, target: str, due: float) -> None:
        """Set expiry for a known live job object, migrating old 24h records."""
        if not target:return
        with self._lock:
            existing=self._conn.execute('SELECT due FROM cleanup WHERE kind=? AND target=?',(kind,target)).fetchone()
            if existing and existing[0]==due:return  # No repeated retention writes every GC cycle.
        self.schedule(kind,target,due)
        with self._lock, self._conn:
            self._conn.execute('UPDATE cleanup SET due=?,next_try=?,attempts=0 WHERE kind=? AND target=?',
                               (due,due,kind,target))

    def protect_copy_until(self,target,expires):
        if not target.startswith('community/submissions/'):raise ValueError('Invalid community copy target')
        with self._lock,self._conn:
            self._conn.execute('INSERT INTO upload_cleanup_guard VALUES(?,?) ON CONFLICT(target) DO UPDATE SET expires=MAX(expires,excluded.expires)',(target,expires+300))

    def cancel(self, kind, target):
        """Publication owns an independent object until explicit withdrawal."""
        if kind!='cos' or not target.startswith('community/submissions/'):
            raise ValueError('Only retained community copies may cancel automatic expiry')
        with self._lock,self._conn:
            self._conn.execute('DELETE FROM cleanup WHERE kind=? AND target=?',(kind,target))
            self._conn.execute('DELETE FROM upload_cleanup_guard WHERE target=?',(target,))

    def close(self) -> None:
        self._conn.close()
