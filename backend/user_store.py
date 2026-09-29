# -*- coding: utf-8 -*-
"""用户、光子余额、配额与审计日志（SQLite，标准库自带，零依赖）。

- users:   openid 为主的用户表，含光子余额与邀请码（余额是扣费唯一依据）；
- audit:   关键动作流水（提交/拦截/限流/完成/奖励），合规要求留存 6 个月以上；
- 每日与每分钟配额、签到/看视频奖励都在 SQLite 里记账，重启不丢。

光子经济（常量即规则，后台 /admin 可改的是价格 prices）：
- 新用户注册赠送 WELCOME_BALANCE 光子；
- 每日签到 +10（1 次/天）、看视频 +10（3 次/天）、邀请一名新用户双方 +30；
- 提交任务按模板价/档位价预扣，任务失败自动全额退款。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 光子经济常量
WELCOME_BALANCE = 90
INVITE_REWARD = 30


def invite_code_of(openid: str) -> str:
    """由 openid 派生稳定邀请码：INV-XXXXXXXX（无需额外存储/索引反查）。"""
    return "INV-" + hashlib.sha256(
        ("inv:" + openid).encode("utf-8")).hexdigest()[:8].upper()


def _local_midnight() -> float:
    lt = time.localtime(time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


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
                CREATE INDEX IF NOT EXISTS idx_audit_user_action_ts
                    ON audit(openid, action, ts);
            """)
            # 老库平滑迁移：补光子余额与邀请码两列
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(users)")}
            if "balance" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0")
            if "invite_code" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN invite_code TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    _USER_COLS = "openid, created_at, last_seen, total_jobs, blocked, balance, invite_code"

    @staticmethod
    def _user_row(row) -> Dict[str, Any]:
        return {"openid": row[0], "created_at": row[1], "last_seen": row[2],
                "total_jobs": row[3], "blocked": row[4],
                "balance": int(row[5] or 0), "invite_code": row[6] or ""}

    def ensure_user(self, openid: str) -> Dict[str, Any]:
        now = time.time()
        code = invite_code_of(openid)
        with self._lock:
            self._conn.execute(
                "INSERT INTO users(openid, created_at, last_seen, balance, invite_code) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(openid) DO UPDATE SET last_seen=excluded.last_seen",
                (openid, now, now, WELCOME_BALANCE, code))
            # 老用户补发邀请码（一次迁移）
            self._conn.execute(
                "UPDATE users SET invite_code=? WHERE openid=? AND invite_code=''",
                (code, openid))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT %s FROM users WHERE openid=?" % self._USER_COLS,
                (openid,)).fetchone()
        return self._user_row(row)

    def get_user(self, openid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT %s FROM users WHERE openid=?" % self._USER_COLS,
                (openid,)).fetchone()
        if row is None:
            return None
        return self._user_row(row)

    # ------------------------------------------------------------------ #
    # 光子余额（扣费的唯一事实来源，客户端数值仅作展示）
    # ------------------------------------------------------------------ #
    def get_balance(self, openid: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()
        return int(row[0] or 0) if row else 0

    def try_spend(self, openid: str, amount: int) -> Tuple[bool, int]:
        """原子扣减光子。余额不足返回 (False, 当前余额)，不产生任何写入。"""
        if amount <= 0:
            return True, self.get_balance(openid)
        with self._lock:
            row = self._conn.execute(
                "SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()
            if row is None:
                return False, 0
            bal = int(row[0] or 0)
            if bal < amount:
                return False, bal
            cur = self._conn.execute(
                "UPDATE users SET balance=balance-? WHERE openid=? AND balance>=?",
                (amount, openid, amount))
            if cur.rowcount != 1:  # 并发扣减被抢空
                self._conn.rollback()
                return False, bal
            self._conn.commit()
            return True, bal - amount

    def add_balance(self, openid: str, delta: int) -> int:
        """加光子（奖励/退款），返回加完后的余额。"""
        with self._lock:
            self._conn.execute(
                "UPDATE users SET balance=balance+? WHERE openid=?",
                (int(delta), openid))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()
        return int(row[0] or 0) if row else 0

    # ------------------------------------------------------------------ #
    # 每日奖励（签到 / 看视频）：次数与发放同事务，刷不掉
    # ------------------------------------------------------------------ #
    def earn(self, openid: str, kind: str, reward: int,
             daily_limit: int) -> Tuple[bool, int, int]:
        """尝试发放 kind 类奖励。返回 (是否成功, 最新余额, 今日已领次数)。"""
        action = "earn_" + kind
        midnight = _local_midnight()
        now = time.time()
        with self._lock:
            cnt = int(self._conn.execute(
                "SELECT COUNT(*) FROM audit "
                "WHERE openid=? AND action=? AND ts>=?",
                (openid, action, midnight)).fetchone()[0])
            row = self._conn.execute(
                "SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()
            bal = int(row[0] or 0) if row else 0
            if cnt >= daily_limit:
                return False, bal, cnt
            self._conn.execute(
                "UPDATE users SET balance=balance+? WHERE openid=?",
                (reward, openid))
            self._conn.execute(
                "INSERT INTO audit(ts, openid, action, detail) VALUES(?,?,?,?)",
                (now, openid, action, "+%d" % reward))
            self._conn.commit()
            return True, bal + reward, cnt + 1

    def earn_count_today(self, openid: str, kind: str) -> int:
        return self.action_count_in_window(
            openid, "earn_" + kind, _local_midnight())

    def action_count(self, openid: str, action: str) -> int:
        return self.action_count_in_window(openid, action, 0.0)

    def action_count_in_window(self, openid: str, action: str,
                               window_start: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM audit "
                "WHERE openid=? AND action=? AND ts>=?",
                (openid, action, window_start)).fetchone()
        return int(row[0])

    def get_by_invite_code(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT %s FROM users WHERE invite_code=?" % self._USER_COLS,
                (code,)).fetchone()
        return self._user_row(row) if row else None

    # ------------------------------------------------------------------ #
    # 计数与审计
    # ------------------------------------------------------------------ #
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

    def jobs_in_window(self, openid: str, window_start: float) -> int:
        return self.action_count_in_window(openid, "submitted", window_start)

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
                "SELECT %s FROM users ORDER BY last_seen DESC LIMIT ?" % self._USER_COLS,
                (limit,)).fetchall()
        return [self._user_row(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        midnight = _local_midnight()
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
