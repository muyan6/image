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


class AdmissionError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


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
                CREATE TABLE IF NOT EXISTS job_charges (
                    job_id TEXT PRIMARY KEY, openid TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK(amount >= 0),
                    state TEXT NOT NULL DEFAULT 'reserved',
                    refunded INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_charges_user_ts
                    ON job_charges(openid, created_at);
                CREATE TABLE IF NOT EXISTS invite_bindings (
                    invitee TEXT PRIMARY KEY, inviter TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ad_rewards (
                    event_id TEXT PRIMARY KEY, openid TEXT NOT NULL,
                    created_at REAL NOT NULL, reward INTEGER NOT NULL
                );
            """)
            # 老库平滑迁移：补光子余额与邀请码两列
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(users)")}
            if "balance" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0")
            if "invite_code" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN invite_code TEXT NOT NULL DEFAULT ''")
            if "banned" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

    def reserve_job(self, openid: str, job_id: str, amount: int,
                    quota: Dict[str, int], free_mode: bool = False) -> int:
        """Balance and quota reservation share one durable, serialized transaction."""
        now = time.time()
        amount = max(0, int(amount))
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT balance, banned FROM users WHERE openid=?", (openid,)).fetchone()
            if row is None or row[1]:
                raise AdmissionError(403, "账号状态不允许提交任务")
            existing = self._conn.execute(
                "SELECT openid FROM job_charges WHERE job_id=?", (job_id,)).fetchone()
            if existing:
                raise AdmissionError(409, "任务已经登记")
            for field, start, message in (
                ("per_minute", now - 60, "提交太频繁，请稍后再试"),
                ("daily", _local_midnight(), "今日次数已用完，明天再来吧"),
            ):
                limit = int(quota.get(field, 0))
                if limit <= 0 or (field == "daily" and free_mode):
                    continue
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM audit WHERE openid=? AND action='submitted' AND ts>=?",
                    (openid, start)).fetchone()[0]
                reserved = self._conn.execute(
                    "SELECT COUNT(*) FROM job_charges WHERE openid=? "
                    "AND state='reserved' AND created_at>=?", (openid, start)).fetchone()[0]
                if count + reserved >= limit:
                    raise AdmissionError(429, message)
            balance = int(row[0])
            if balance < amount:
                raise AdmissionError(402, "光子不足：本次需要 %d ✦，当前余额 %d ✦" % (amount, balance))
            self._conn.execute("UPDATE users SET balance=balance-? WHERE openid=?", (amount, openid))
            self._conn.execute("INSERT INTO job_charges(job_id,openid,amount,created_at) VALUES(?,?,?,?)",
                               (job_id, openid, amount, now))
            return balance - amount

    def confirm_job(self, job_id: str, detail: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT openid,state FROM job_charges WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row[1] != "reserved":
                return
            self._conn.execute("UPDATE job_charges SET state='submitted' WHERE job_id=?", (job_id,))
            self._conn.execute("UPDATE users SET total_jobs=total_jobs+1 WHERE openid=?", (row[0],))
            self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                               (time.time(), row[0], "submitted", detail[:500]))

    def charged_amount(self, job_id: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute("SELECT amount FROM job_charges WHERE job_id=?", (job_id,)).fetchone()
        return int(row[0]) if row else None

    def refund_job(self, openid: str, job_id: str, cancel: bool = False) -> int:
        """Exactly-once refund from the actual debit ledger, never current prices."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT amount,refunded FROM job_charges WHERE job_id=? AND openid=?",
                                     (job_id, openid)).fetchone()
            if row and not row[1]:
                amount = int(row[0])
                self._conn.execute("UPDATE users SET balance=balance+? WHERE openid=?", (amount, openid))
                self._conn.execute("UPDATE job_charges SET refunded=1,state=? WHERE job_id=?",
                                   ("cancelled" if cancel else "failed", job_id))
                if amount:
                    self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                                       (time.time(), openid, "refund", "job=%s +=%d" % (job_id, amount)))
            if row and cancel:
                self._conn.execute("UPDATE job_charges SET state='cancelled' WHERE job_id=?", (job_id,))
                removed = self._conn.execute("DELETE FROM audit WHERE openid=? AND action='submitted' "
                                             "AND detail LIKE ?", (openid, "job=" + job_id + " %")).rowcount
                if removed:
                    self._conn.execute("UPDATE users SET total_jobs=MAX(0,total_jobs-1) WHERE openid=?", (openid,))
            result = self._conn.execute("SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()
            return int(result[0]) if result else 0

    def reconcile_charges(self, statuses: Dict[str, str]) -> None:
        """Recover debits left between the user DB commit and job DB commit."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id,openid,state FROM job_charges WHERE refunded=0 "
                "AND state IN ('reserved','submitted')").fetchall()
        for job_id, openid, state in rows:
            status = statuses.get(job_id)
            if status in (None, "failed", "deleted"):
                self.refund_job(openid, job_id, cancel=status is None and state == "reserved")
            elif status == "succeeded":
                self.complete_charge(job_id)

    def complete_charge(self, job_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE job_charges SET state='succeeded' WHERE job_id=? AND refunded=0", (job_id,))

    def bind_invite_once(self, openid: str, code: str) -> Tuple[int, int]:
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            inviter = self._conn.execute("SELECT openid FROM users WHERE invite_code=?", (code,)).fetchone()
            if inviter is None or inviter[0] == openid:
                raise ValueError("邀请码不存在或属于自己")
            # Honor historical audit rows when upgrading an existing installation.
            bound = self._conn.execute("SELECT 1 FROM audit WHERE openid=? AND action='invite_bound' LIMIT 1", (openid,)).fetchone()
            if bound or self._conn.execute("SELECT 1 FROM invite_bindings WHERE invitee=?", (openid,)).fetchone():
                raise ValueError("已经绑定过邀请码了")
            self._conn.execute("INSERT INTO invite_bindings VALUES(?,?,?)", (openid, inviter[0], time.time()))
            for target, action, detail in ((openid, "invite_bound", "by=" + code),
                                            (inviter[0], "invite_reward", "invitee=%s***" % openid[:6])):
                self._conn.execute("UPDATE users SET balance=balance+? WHERE openid=?", (INVITE_REWARD, target))
                self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                                   (time.time(), target, action, detail))
            return tuple(int(self._conn.execute("SELECT balance FROM users WHERE openid=?", (target,)).fetchone()[0])
                         for target in (openid, inviter[0]))

    def claim_video_reward(self, openid: str, event_id: str, reward: int, limit: int) -> Tuple[bool, int, int]:
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            prior = self._conn.execute("SELECT openid FROM ad_rewards WHERE event_id=?", (event_id,)).fetchone()
            count = self._conn.execute("SELECT COUNT(*) FROM audit WHERE openid=? AND action='earn_video' AND ts>=?",
                                       (openid, _local_midnight())).fetchone()[0]
            balance = self._conn.execute("SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()[0]
            if prior:
                if prior[0] != openid:
                    raise ValueError("广告凭据已经使用")
                return True, int(balance), count
            if count >= limit:
                return False, int(balance), count
            self._conn.execute("INSERT INTO ad_rewards VALUES(?,?,?,?)", (event_id, openid, time.time(), reward))
            self._conn.execute("UPDATE users SET balance=balance+? WHERE openid=?", (reward, openid))
            self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                               (time.time(), openid, "earn_video", "event=%s +%d" % (event_id, reward)))
            return True, int(balance) + reward, count + 1

    _USER_COLS = ("openid, created_at, last_seen, total_jobs, blocked, "
                  "balance, invite_code, banned")

    @staticmethod
    def _user_row(row) -> Dict[str, Any]:
        return {"openid": row[0], "created_at": row[1], "last_seen": row[2],
                "total_jobs": row[3], "blocked": row[4],
                "balance": int(row[5] or 0), "invite_code": row[6] or "",
                "banned": bool(row[7])}

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

    def set_balance(self, openid: str, value: int) -> int:
        """管理员直接设置余额（后台改光子用），返回设置后的余额。"""
        with self._lock:
            self._conn.execute(
                "UPDATE users SET balance=? WHERE openid=?",
                (max(0, int(value)), openid))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()
        return int(row[0] or 0) if row else 0

    def set_banned(self, openid: str, banned: bool) -> None:
        """封禁/解封：封禁后无法提交任务（登录与历史查看不受影响）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE users SET banned=? WHERE openid=?",
                (1 if banned else 0, openid))
            self._conn.commit()

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
