# -*- coding: utf-8 -*-
"""用户、配额与审计日志（SQLite，标准库自带，零依赖）。

- users:   openid 为主的用户表，统计提交/被拦截次数；
- audit:   关键动作流水（提交/拦截/限流/完成），合规要求留存 6 个月以上；
- 每日与每分钟配额在 SQLite 里计数，重启不丢。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


class UserStore:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "users.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    openid      TEXT PRIMARY KEY,
                    created_at  REAL NOT NULL,
                    last_seen   REAL NOT NULL,
                    total_jobs  INTEGER NOT NULL DEFAULT 0,
                    blocked     INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts     REAL NOT NULL,
                    openid TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
                CREATE INDEX IF NOT EXISTS idx_audit_openid ON audit(openid);
            """)
            self._conn.commit()

    # ------------------------------------------------------------------ #
    def ensure_user(self, openid: str) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users(openid, created_at, last_seen) VALUES(?,?,?) "
                "ON CONFLICT(openid) DO UPDATE SET last_seen=excluded.last_seen",
                (openid, now, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT openid, created_at, last_seen, total_jobs, blocked "
                "FROM users WHERE openid=?", (openid,)).fetchone()
        return {"openid": row[0], "created_at": row[1], "last_seen": row[2],
                "total_jobs": row[3], "blocked": row[4]}

    def get_user(self, openid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT openid, created_at, last_seen, total_jobs, blocked "
                "FROM users WHERE openid=?", (openid,)).fetchone()
        if row is None:
            return None
        return {"openid": row[0], "created_at": row[1], "last_seen": row[2],
                "total_jobs": row[3], "blocked": row[4]}

    def inc_total(self, openid: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET total_jobs=total_jobs+1 WHERE openid=?", (openid,))
            self._conn.commit()

    def inc_blocked(self, openid: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET blocked=blocked+1 WHERE openid=?", (openid,))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 配额
    # ------------------------------------------------------------------ #
    def jobs_in_window(self, openid: str, window_start: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM audit "
                "WHERE openid=? AND action='submitted' AND ts>=?",
                (openid, window_start)).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    def audit(self, openid: str, action: str, detail: str = "") -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO audit(ts, openid, action, detail) VALUES(?,?,?,?)",
                    (time.time(), openid, action, detail[:500]))
                self._conn.commit()
        except sqlite3.Error as exc:  # 审计失败不阻断业务，但要大声
            import logging
            logging.getLogger("rescue.audit").error("审计写入失败: %s", exc)

    def recent_audit(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, openid, action, detail FROM audit "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "openid": r[1], "action": r[2], "detail": r[3]}
                for r in rows]

    def list_users(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT openid, created_at, last_seen, total_jobs, blocked "
                "FROM users ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [{"openid": r[0], "created_at": r[1], "last_seen": r[2],
                 "total_jobs": r[3], "blocked": r[4]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        now = time.time()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        with self._lock:
            users_total = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            users_today = self._conn.execute(
                "SELECT COUNT(*) FROM users WHERE created_at>=?", (midnight,)).fetchone()[0]
            blocked_today = self._conn.execute(
                "SELECT COUNT(*) FROM audit WHERE action='blocked' AND ts>=?",
                (midnight,)).fetchone()[0]
        return {"users_total": users_total, "users_today": users_today,
                "blocked_today": blocked_today}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
