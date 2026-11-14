"""SQLite 存储层：账号、领取记录、设置、日志。

单进程 asyncio 服务，sqlite3 的同步调用都是毫秒级，直接在当前线程用，
加一把锁避免并发写冲突。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import DATA_DIR, DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init() -> None:
    with _lock:
        c = _connect()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                login TEXT,
                display_name TEXT,
                twitch_user_id TEXT,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                token_expires_at TEXT,
                token_kind TEXT DEFAULT 'device',
                device_id TEXT,
                enabled INTEGER DEFAULT 1,
                mode TEXT DEFAULT 'farm',
                games TEXT DEFAULT '[]',
                proxy TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                drop_id TEXT,
                campaign_id TEXT,
                campaign_name TEXT,
                game TEXT,
                drop_name TEXT,
                rewards TEXT,
                claimed_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_unique
                ON claims (account_id, drop_id);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                account_id INTEGER,
                level TEXT,
                message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs (id DESC);
            CREATE TABLE IF NOT EXISTS watch_stats (
                account_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                seconds INTEGER DEFAULT 0,
                PRIMARY KEY (account_id, day)
            );
            """
        )
        c.commit()


# ---------------------------------------------------------------- 设置
def get_setting(key: str, default: Any = None) -> Any:
    with _lock:
        row = _connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        c.commit()


# ---------------------------------------------------------------- 账号
def _account_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["games"] = json.loads(d.get("games") or "[]")
    except Exception:
        d["games"] = []
    d["enabled"] = bool(d.get("enabled"))
    return d


def list_accounts() -> list[dict]:
    with _lock:
        rows = _connect().execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [_account_row(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    with _lock:
        row = _connect().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return _account_row(row) if row else None


def create_account(**fields: Any) -> dict:
    fields.setdefault("created_at", now_iso())
    fields.setdefault("updated_at", now_iso())
    if isinstance(fields.get("games"), list):
        fields["games"] = json.dumps(fields["games"])
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with _lock:
        c = _connect()
        cur = c.execute(f"INSERT INTO accounts ({cols}) VALUES ({marks})", tuple(fields.values()))
        c.commit()
        account_id = cur.lastrowid
    return get_account(account_id)  # type: ignore[return-value]


def update_account(account_id: int, **fields: Any) -> dict | None:
    if not fields:
        return get_account(account_id)
    if isinstance(fields.get("games"), list):
        fields["games"] = json.dumps(fields["games"])
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        c = _connect()
        c.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), account_id))
        c.commit()
    return get_account(account_id)


def delete_account(account_id: int) -> None:
    with _lock:
        c = _connect()
        c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        c.execute("DELETE FROM claims WHERE account_id=?", (account_id,))
        c.commit()


# ---------------------------------------------------------------- 领取记录
def add_claim(account_id: int, drop_id: str, campaign_id: str, campaign_name: str,
              game: str, drop_name: str, rewards: list[str]) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR IGNORE INTO claims (account_id, drop_id, campaign_id, campaign_name, "
            "game, drop_name, rewards, claimed_at) VALUES (?,?,?,?,?,?,?,?)",
            (account_id, drop_id, campaign_id, campaign_name, game, drop_name,
             json.dumps(rewards, ensure_ascii=False), now_iso()),
        )
        c.commit()


def account_stats(account_id: int) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    with _lock:
        c = _connect()
        total = c.execute("SELECT COUNT(*) FROM claims WHERE account_id=?", (account_id,)).fetchone()[0]
        recent = c.execute(
            "SELECT COUNT(*) FROM claims WHERE account_id=? AND claimed_at>=?",
            (account_id, since),
        ).fetchone()[0]
    return {"claimed_total": total, "claimed_24h": recent}


def add_watch_seconds(account_id: int, seconds: int) -> None:
    if seconds <= 0:
        return
    day = datetime.now(timezone.utc).date().isoformat()
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO watch_stats (account_id, day, seconds) VALUES (?,?,?) "
            "ON CONFLICT(account_id, day) DO UPDATE SET seconds = seconds + excluded.seconds",
            (account_id, day, int(seconds)),
        )
        c.commit()


def watch_minutes_24h(account_id: int) -> int:
    days = [
        (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()
        for n in range(2)
    ]
    marks = ",".join("?" * len(days))
    with _lock:
        row = _connect().execute(
            f"SELECT COALESCE(SUM(seconds),0) FROM watch_stats WHERE account_id=? AND day IN ({marks})",
            (account_id, *days),
        ).fetchone()
    return int(row[0] // 60)


def recent_claims(limit: int = 50) -> list[dict]:
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM claims ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["rewards"] = json.loads(d.get("rewards") or "[]")
        except Exception:
            d["rewards"] = []
        out.append(d)
    return out


# ---------------------------------------------------------------- 日志
def add_log(message: str, account_id: int | None = None, level: str = "info") -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO logs (ts, account_id, level, message) VALUES (?,?,?,?)",
            (now_iso(), account_id, level, message),
        )
        # 只保留最近 3000 条
        c.execute("DELETE FROM logs WHERE id < (SELECT MAX(id) - 3000 FROM logs)")
        c.commit()


def list_logs(limit: int = 200, account_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM logs"
    args: list[Any] = []
    if account_id is not None:
        sql += " WHERE account_id=?"
        args.append(account_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _connect().execute(sql, args).fetchall()
    return [dict(r) for r in rows]
