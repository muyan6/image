# -*- coding: utf-8 -*-
"""用户、光子余额、配额与审计日志（SQLite，标准库自带，零依赖）。

- users:   openid 为主的用户表，含光子余额与邀请码（余额是扣费唯一依据）；
- audit:   关键动作流水（提交/拦截/限流/完成/奖励），合规要求留存 6 个月以上；
- 每日与每分钟配额、签到/看视频奖励都在 SQLite 里记账，重启不丢。

光子经济（常量即规则，后台 /admin 可改的是价格 prices）：
- 新用户注册赠送 WELCOME_BALANCE 光子；
- 每日签到与邀请奖励由后台配置；看视频默认 +10（3 次/天）。
- 提交任务按模板价/档位价预扣，任务失败自动全额退款。
"""
from __future__ import annotations

import hashlib
import datetime
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 光子经济常量
WELCOME_BALANCE = 100
INVITE_REWARD = 40
ACCOUNT_TYPES = ("wechat", "web")


def public_user_id(openid: str, account_type: str = "wechat") -> str:
    """Stable display ID derived from the server identity, not client storage."""
    prefix = "WEB-" if account_type == "web" else "WX-"
    return prefix + hashlib.sha256(("rescue-user:" + openid).encode("utf-8")).hexdigest()[:16].upper()


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
        self._purging = False
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
                CREATE TABLE IF NOT EXISTS violations (
                    id TEXT PRIMARY KEY, openid TEXT NOT NULL, created_at REAL NOT NULL,
                    kind TEXT NOT NULL, reason TEXT NOT NULL, charged INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', feedback TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_violations_user_ts
                    ON violations(openid, created_at);
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
            if "auto_banned" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN auto_banned INTEGER NOT NULL DEFAULT 0")
            if "account_type" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN account_type TEXT NOT NULL DEFAULT ''")
            if "app_id" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN app_id TEXT NOT NULL DEFAULT ''")
            if "admin_hidden" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN admin_hidden INTEGER NOT NULL DEFAULT 0")
            if "nickname" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN nickname TEXT NOT NULL DEFAULT ''")
            # Historical auth had exactly two namespaces. Keep every balance/job;
            # split IP-derived web visitors from mini-program accounts, don't merge.
            self._conn.execute("UPDATE users SET account_type='web' WHERE account_type='' AND openid LIKE 'web-%'")
            self._conn.execute("UPDATE users SET account_type='wechat' WHERE account_type=''")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_users_source_active ON users(account_type,last_seen)")
            self._conn.commit()

    def reserve_job(self, openid: str, job_id: str, amount: int,
                    quota: Dict[str, int], free_mode: bool = False) -> int:
        """Balance and quota reservation share one durable, serialized transaction."""
        now = time.time()
        amount = max(0, int(amount))
        with self._lock, self._conn:
            if self._purging:
                raise AdmissionError(503, "账号清理中，请稍后再试")
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

    def record_violation(self, openid: str, violation_id: str, kind: str,
                         reason: str, price: int) -> Dict[str, Any]:
        """审核确定拦截后，原子记录、扣点和滚动七日封禁。服务故障不调用此方法。"""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()
            if row is None:
                raise ValueError("账号不存在")
            charge = min(max(0, int(price)), int(row[0]))
            self._conn.execute("UPDATE users SET balance=balance-?,blocked=blocked+1 WHERE openid=?",
                               (charge, openid))
            self._conn.execute("INSERT INTO violations VALUES(?,?,?,?,?,?,?,?)",
                               (violation_id, openid, now, kind, reason[:200], charge, "active", ""))
            count = self._conn.execute(
                "SELECT COUNT(*) FROM violations WHERE openid=? AND status IN ('active','upheld') AND created_at>=?",
                (openid, now - 7 * 86400)).fetchone()[0]
            banned = count >= 3
            if banned:
                self._conn.execute("UPDATE users SET banned=1,auto_banned=1 WHERE openid=? AND banned=0", (openid,))
            self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                               (now, openid, "blocked", "id=%s kind=%s charged=%d" % (violation_id, kind, charge)))
            return {"violation_id": violation_id, "charged": charge,
                    "balance": int(row[0]) - charge, "weekly_count": count, "banned": banned}

    def submit_violation_feedback(self, openid: str, violation_id: str, message: str) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT status,feedback FROM violations WHERE id=? AND openid=?",
                                     (violation_id, openid)).fetchone()
            if not row or row[0] != "active" or row[1]:
                return False
            self._conn.execute("UPDATE violations SET feedback=? WHERE id=?", (message[:500], violation_id))
            self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                               (time.time(), openid, "violation_feedback", "id=" + violation_id))
            return True

    def list_violations(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT id,openid,created_at,kind,reason,charged,status,feedback "
                                      "FROM violations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("id", "openid", "created_at", "kind", "reason", "charged", "status", "feedback"), r))
                for r in rows]

    def resolve_violation(self, violation_id: str, accepted: bool) -> Optional[Dict[str, Any]]:
        """管理员确认误判时原路退回处罚点数，并在无有效违规时解除自动封禁。"""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT openid,charged,status FROM violations WHERE id=?",
                                     (violation_id,)).fetchone()
            if not row:
                return None
            openid, charged, status = row
            if status != "active":
                return {"status": status, "balance": self._conn.execute(
                    "SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()[0]}
            next_status = "overturned" if accepted else "upheld"
            self._conn.execute("UPDATE violations SET status=? WHERE id=?", (next_status, violation_id))
            if accepted:
                self._conn.execute("UPDATE users SET balance=balance+?,blocked=MAX(0,blocked-1) WHERE openid=?",
                                   (charged, openid))
                remaining = self._conn.execute("SELECT COUNT(*) FROM violations WHERE openid=? "
                                               "AND status IN ('active','upheld') AND created_at>=?",
                                               (openid, time.time() - 7 * 86400)).fetchone()[0]
                if remaining < 3:
                    self._conn.execute("UPDATE users SET banned=0,auto_banned=0 WHERE openid=? AND auto_banned=1", (openid,))
            self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                               (time.time(), openid, "violation_review", "id=%s status=%s" % (violation_id, next_status)))
            balance = self._conn.execute("SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()[0]
            return {"status": next_status, "balance": int(balance)}

    def bind_invite_once(self, openid: str, code: str, reward: int = INVITE_REWARD) -> Tuple[int, int]:
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
                self._conn.execute("UPDATE users SET balance=balance+? WHERE openid=?", (reward, target))
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
                  "balance, invite_code, banned, account_type, app_id, admin_hidden, nickname")

    @staticmethod
    def _user_row(row) -> Dict[str, Any]:
        return {"openid": row[0], "created_at": row[1], "last_seen": row[2],
                "total_jobs": row[3], "blocked": row[4],
                "balance": int(row[5] or 0), "invite_code": row[6] or "",
                "banned": bool(row[7]), "account_type": row[8], "app_id": row[9],
                "admin_hidden": bool(row[10]), "nickname": row[11] or "",
                "user_id": public_user_id(row[0], row[8])}

    def ensure_user(self, openid: str, account_type: Optional[str] = None,
                    app_id: str = "") -> Dict[str, Any]:
        now = time.time()
        code = invite_code_of(openid)
        kind = account_type or ("web" if openid.startswith("web-") else "wechat")
        if kind not in ACCOUNT_TYPES:
            raise ValueError("未知的账号来源")
        with self._lock, self._conn:
            if self._purging:
                raise ValueError("账号清理中，请稍后再试")
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute("SELECT account_type,app_id FROM users WHERE openid=?", (openid,)).fetchone()
            if existing and (existing[0] != kind or (app_id and existing[1] and existing[1] != app_id)):
                raise ValueError("账号身份与小程序归属不一致，请核对 AppID")
            self._conn.execute(
                "INSERT INTO users(openid, created_at, last_seen, balance, invite_code,account_type,app_id) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(openid) DO UPDATE SET last_seen=excluded.last_seen,admin_hidden=0,"
                "app_id=CASE WHEN users.app_id='' THEN excluded.app_id ELSE users.app_id END",
                (openid, now, now, WELCOME_BALANCE, code, kind, app_id))
            # 老用户补发邀请码（一次迁移）
            self._conn.execute(
                "UPDATE users SET invite_code=? WHERE openid=? AND invite_code=''",
                (code, openid))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT %s FROM users WHERE openid=?" % self._USER_COLS,
                (openid,)).fetchone()
        return self._user_row(row)

    def touch_user(self, openid: str) -> None:
        """Activity is not registration; cap writes to one per five minutes."""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET last_seen=?,admin_hidden=0 "
                               "WHERE openid=? AND (last_seen<? OR admin_hidden=1)", (now, openid, now - 300))

    def set_nickname(self, openid: str, nickname: str) -> str:
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET nickname=? WHERE openid=?", (nickname, openid))
        return nickname

    def hide_from_admin(self, openid: str) -> bool:
        """Remove from management lists, not authentication, balances, works or bans."""
        with self._lock, self._conn:
            found = self._conn.execute("UPDATE users SET admin_hidden=1 WHERE openid=?", (openid,)).rowcount
            if found:
                self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                                   (time.time(), openid, "admin_cleanup_user", "仅移除管理列表，再次活跃自动恢复"))
            return bool(found)

    def admin_remove_summary(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT account_type,COUNT(*) FROM users WHERE admin_hidden=0 GROUP BY account_type").fetchall()
        counts = dict(rows)
        return {"accounts":sum(counts.values()),"wechat":counts.get("wechat",0),"web":counts.get("web",0)}

    def hide_all_from_admin(self, expected_accounts: int) -> int:
        """Only remove management visibility; no account, ledger or job deletion."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            count = self._conn.execute("SELECT COUNT(*) FROM users WHERE admin_hidden=0").fetchone()[0]
            if count != expected_accounts:
                raise ValueError("可见用户数量已变化，请刷新后重新确认")
            self._conn.execute("UPDATE users SET admin_hidden=1 WHERE admin_hidden=0")
            self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                               (time.time(), "", "admin_cleanup_all_users", "移出管理列表 %d 个账号，保留所有账户数据" % count))
            return int(count)

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
                "UPDATE users SET banned=?,auto_banned=0 WHERE openid=?",
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

    def _checkin_status_locked(self, openid: str, now: float) -> Dict[str, Any]:
        today = datetime.date.fromtimestamp(now).toordinal()
        rows = self._conn.execute("SELECT ts FROM audit WHERE openid=? AND action='earn_checkin'",
                                  (openid,)).fetchall()
        days = {datetime.date.fromtimestamp(ts).toordinal() for (ts,) in rows}
        done = today in days
        cursor = today if done else today - 1
        streak = 0
        while cursor in days:
            streak += 1
            cursor -= 1
        progress = ((streak - 1) % 7 + 1) if done and streak else streak % 7
        return {"checkin_done": done, "checkin_streak": streak,
                "checkin_progress": progress,
                "checkin_day": progress if done else progress + 1}

    def checkin_status(self, openid: str) -> Dict[str, Any]:
        with self._lock:
            return self._checkin_status_locked(openid, time.time())

    def claim_checkin(self, openid: str, reward: int, seventh_bonus: int) -> Dict[str, Any]:
        """每日一次与七日奖励在同一事务完成；重复请求不重复发放。"""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            status = self._checkin_status_locked(openid, now)
            balance_row = self._conn.execute("SELECT balance FROM users WHERE openid=?", (openid,)).fetchone()
            if balance_row is None:
                raise ValueError("账号不存在")
            if status["checkin_done"]:
                return {**status, "claimed": False, "balance": int(balance_row[0]),
                        "reward": 0, "bonus_awarded": 0}
            streak = status["checkin_streak"] + 1
            bonus = seventh_bonus if streak % 7 == 0 else 0
            total = reward + bonus
            self._conn.execute("UPDATE users SET balance=balance+? WHERE openid=?", (total, openid))
            self._conn.execute("INSERT INTO audit(ts,openid,action,detail) VALUES(?,?,?,?)",
                               (now, openid, "earn_checkin", "+%d streak=%d bonus=%d" % (total, streak, bonus)))
            return {"claimed": True, "balance": int(balance_row[0]) + total,
                    "reward": total, "bonus_awarded": bonus, "checkin_done": True,
                    "checkin_streak": streak, "checkin_progress": (streak - 1) % 7 + 1,
                    "checkin_day": (streak - 1) % 7 + 1}

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

    def list_users(self, limit: int = 50, account_type: str = "wechat") -> List[Dict[str, Any]]:
        if account_type not in (*ACCOUNT_TYPES, "all"):
            raise ValueError("账号来源只能是 wechat、web 或 all")
        with self._lock:
            where = " WHERE admin_hidden=0" + ("" if account_type == "all" else " AND account_type=?")
            params = (limit,) if account_type == "all" else (account_type, limit)
            rows = self._conn.execute(
                "SELECT %s FROM users%s ORDER BY last_seen DESC LIMIT ?" % (self._USER_COLS, where), params).fetchall()
        return [self._user_row(r) for r in rows]

    def purge_summary(self) -> Dict[str, int]:
        """包含隐藏账号的真实总数；删除确认不能依赖分页管理列表。"""
        with self._lock:
            rows = self._conn.execute("SELECT account_type,COUNT(*) FROM users GROUP BY account_type").fetchall()
        counts = dict(rows)
        return {"accounts": sum(counts.values()), "wechat": counts.get("wechat", 0),
                "web": counts.get("web", 0)}

    def begin_purge(self) -> None:
        with self._lock:
            if self._purging:
                raise ValueError("账号清理正在进行")
            self._purging = True

    def end_purge(self) -> None:
        with self._lock:
            self._purging = False

    def purge_all_accounts(self) -> Dict[str, int]:
        """原子删除全部账号和账户关联记录，不把封禁/隐藏当作删除。"""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            summary = self.purge_summary_unlocked()
            for table in ("violations", "ad_rewards", "invite_bindings", "job_charges", "audit", "users"):
                self._conn.execute("DELETE FROM " + table)
            return summary

    def purge_summary_unlocked(self) -> Dict[str, int]:
        rows = self._conn.execute("SELECT account_type,COUNT(*) FROM users GROUP BY account_type").fetchall()
        counts = dict(rows)
        return {"accounts": sum(counts.values()), "wechat": counts.get("wechat", 0),
                "web": counts.get("web", 0)}

    def stats(self) -> Dict[str, Any]:
        midnight = _local_midnight()
        with self._lock:
            counts = {kind: (count, today) for kind, count, today in self._conn.execute(
                "SELECT account_type,COUNT(*),SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) "
                "FROM users WHERE admin_hidden=0 GROUP BY account_type", (midnight,)).fetchall()}
            wechat_total, wechat_today = counts.get("wechat", (0, 0))
            web_total, web_today = counts.get("web", (0, 0))
            blocked_today = self._conn.execute(
                "SELECT COUNT(*) FROM audit WHERE action='blocked' AND ts>=?",
                (midnight,)).fetchone()[0]
        return {"users_total": wechat_total, "users_today": wechat_today,
                "wechat_users_total": wechat_total, "wechat_users_today": wechat_today,
                "web_users_total": web_total, "web_users_today": web_today,
                "accounts_total": wechat_total + web_total,
                "blocked_today": blocked_today}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
