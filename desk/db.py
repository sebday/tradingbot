"""SQLite log. Every order stores the trade and the short reason Grok gave."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .config import state_dir


def db_path() -> Path:
    override = os.environ.get("DESK_DB", "").strip()
    return Path(override) if override else state_dir() / "grok.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          side TEXT NOT NULL,
          pair_id TEXT NOT NULL,
          wsname TEXT,
          size_usd REAL,
          qty REAL,
          price REAL,
          order_type TEXT,
          reason TEXT NOT NULL,
          rule_name TEXT,
          rule_params TEXT,
          txid TEXT,
          status TEXT NOT NULL,
          live INTEGER NOT NULL,
          entry_id INTEGER,
          raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS exit_rules (
          id INTEGER PRIMARY KEY,
          pair_id TEXT NOT NULL,
          opened_ts TEXT,
          name TEXT NOT NULL,
          params_json TEXT NOT NULL,
          source TEXT NOT NULL,
          reason TEXT NOT NULL,
          set_at TEXT NOT NULL,
          active INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          pair_id TEXT NOT NULL,
          wsname TEXT,
          size_usd REAL NOT NULL,
          kind TEXT NOT NULL,
          params_json TEXT NOT NULL,
          exit_rule_json TEXT NOT NULL,
          reason TEXT NOT NULL,
          expires_at TEXT,
          kraken_txid TEXT,
          status TEXT NOT NULL,
          live INTEGER NOT NULL,
          last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS wakes (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          session_id TEXT,
          status TEXT,
          summary TEXT
        );
        """
    )
    return conn


def require_reason(reason: str) -> str:
    text = " ".join((reason or "").split())
    if len(text) < 3:
        raise SystemExit("a short --reason is required")
    return text[:500]


class Log:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or connect()

    def add_trade(self, row: dict) -> int:
        row = dict(row)
        row["reason"] = require_reason(str(row.get("reason") or ""))
        cols = (
            "ts", "side", "pair_id", "wsname", "size_usd", "qty", "price", "order_type",
            "reason", "rule_name", "rule_params", "txid", "status", "live", "entry_id", "raw_json",
        )
        cur = self.conn.execute(
            f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [row.get(c) for c in cols],
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_trades(self, n: int = 20) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(n),)).fetchall()
        return [dict(r) for r in rows]

    def set_rule(self, row: dict) -> int:
        reason = require_reason(str(row.get("reason") or ""))
        self.conn.execute("UPDATE exit_rules SET active=0 WHERE pair_id=? AND active=1", (row["pair_id"],))
        cur = self.conn.execute(
            """INSERT INTO exit_rules (pair_id, opened_ts, name, params_json, source, reason, set_at, active)
               VALUES (?,?,?,?,?,?,?,1)""",
            (
                row["pair_id"], row.get("opened_ts"), row["name"],
                json.dumps(row.get("params") or {}, separators=(",", ":")),
                row.get("source") or "library", reason, row["set_at"],
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def active_rules(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM exit_rules WHERE active=1 ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def bind_rule(self, rule_id: int, opened_ts: str) -> None:
        self.conn.execute("UPDATE exit_rules SET opened_ts=? WHERE id=?", (opened_ts, rule_id))
        self.conn.commit()

    def deactivate_rule(self, rule_id: int) -> None:
        self.conn.execute("UPDATE exit_rules SET active=0 WHERE id=?", (rule_id,))
        self.conn.commit()

    def add_entry(self, row: dict) -> int:
        reason = require_reason(str(row.get("reason") or ""))
        cur = self.conn.execute(
            """INSERT INTO entries
               (ts, pair_id, wsname, size_usd, kind, params_json, exit_rule_json, reason,
                expires_at, kraken_txid, status, live, last_error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["ts"], row["pair_id"], row.get("wsname"), float(row["size_usd"]), row["kind"],
                json.dumps(row.get("params") or {}, separators=(",", ":")),
                json.dumps(row.get("exit_rule") or {}, separators=(",", ":")),
                reason, row.get("expires_at"), row.get("kraken_txid"), row.get("status") or "waiting",
                1 if row.get("live") else 0, row.get("last_error"),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def waiting_entries(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM entries WHERE status='waiting' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_entry(self, entry_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{key}=?" for key in fields)
        self.conn.execute(f"UPDATE entries SET {cols} WHERE id=?", (*fields.values(), entry_id))
        self.conn.commit()

    def add_wake(self, ts: str, session_id: str | None, status: str, summary: str) -> None:
        self.conn.execute(
            "INSERT INTO wakes (ts, session_id, status, summary) VALUES (?,?,?,?)",
            (ts, session_id, status, (summary or "")[:2000]),
        )
        self.conn.commit()
