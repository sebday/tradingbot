"""SQLite journal: the system of record. The conversation is not."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from . import paths
from .util import utc_now

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS runs (
  cycle_id TEXT PRIMARY KEY, kind TEXT NOT NULL, mode TEXT, status TEXT,
  started_at TEXT, finished_at TEXT, receipt_path TEXT, receipt_json TEXT);
CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY, cycle_id TEXT, ts TEXT, tool TEXT, args_json TEXT,
  ok INTEGER, result_json TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS tool_calls_cycle ON tool_calls(cycle_id);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, submission INTEGER, idx INTEGER, ts TEXT,
  action TEXT, pair_id TEXT, size_usd REAL, stop_price REAL, thesis TEXT, invalidation TEXT,
  evidence_json TEXT, raw_json TEXT, valid INTEGER, errors_json TEXT, final INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS proposals_cycle ON proposals(cycle_id);
CREATE TABLE IF NOT EXISTS gate_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER, cycle_id TEXT, ts TEXT,
  dry_run INTEGER, approved INTEGER, reasons_json TEXT, size_usd REAL, limit_price REAL,
  stop_price REAL, ask REAL);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, ts TEXT, venue TEXT, pair_id TEXT, side TEXT,
  ordertype TEXT, price REAL, volume REAL, oflags TEXT, timeinforce TEXT, status TEXT, txid TEXT,
  proposal_id INTEGER, source TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, cycle_id TEXT, venue TEXT, pair_id TEXT, wsname TEXT,
  qty REAL, cost_usd REAL, price REAL, fee_usd REAL, txid TEXT, order_id INTEGER, source TEXT,
  dedupe TEXT UNIQUE, raw_json TEXT);
CREATE INDEX IF NOT EXISTS fills_venue ON fills(venue, pair_id);
CREATE TABLE IF NOT EXISTS closes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, cycle_id TEXT, venue TEXT, pair_id TEXT, wsname TEXT,
  qty REAL, proceeds_usd REAL, price REAL, fee_usd REAL, rule_fired TEXT, txid TEXT, status TEXT,
  order_id INTEGER, source TEXT, dedupe TEXT UNIQUE, raw_json TEXT);
CREATE INDEX IF NOT EXISTS closes_venue ON closes(venue, pair_id);
CREATE TABLE IF NOT EXISTS stops (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, venue TEXT, pair_id TEXT, episode_opened_ts TEXT,
  stop_price REAL, source TEXT, cycle_id TEXT);
CREATE INDEX IF NOT EXISTS stops_ep ON stops(venue, pair_id, episode_opened_ts);
CREATE TABLE IF NOT EXISTS grades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, subject TEXT, subject_id INTEGER, cycle_id TEXT,
  pair_id TEXT, action TEXT, approved INTEGER, p0 REAL, ret_1h REAL, ret_6h REAL, ret_24h REAL,
  mae_24h REAL, stop_hit INTEGER, btc_24h REAL, baseline_pair TEXT, baseline_24h REAL, raw_json TEXT,
  UNIQUE(subject, subject_id));
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, cycle_id TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, detail TEXT, check_name TEXT,
  test_name TEXT, cycle_id TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, cycle_id TEXT, payload_json TEXT);
"""

TABLES = ("runs", "tool_calls", "proposals", "gate_decisions", "orders", "fills", "closes",
          "grades", "notes", "incidents", "stops", "events", "meta")


def _j(v) -> str | None:
    return None if v is None else json.dumps(v, separators=(",", ":"), default=str)


class Journal:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else paths.db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL") if str(self.path) != ":memory:" else None
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        if self.meta_get("schema_version") is None:
            self.meta_set("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ helpers
    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.db.execute(sql, args))

    def x(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self.db.execute(sql, args)
            return cur.lastrowid

    def meta_get(self, k: str) -> str | None:
        rows = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return rows[0]["v"] if rows else None

    def meta_set(self, k: str, v: str | None) -> None:
        if v is None:
            self.x("DELETE FROM meta WHERE k=?", (k,))
        else:
            self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def meta_json(self, k: str, default=None):
        raw = self.meta_get(k)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def schema_version(self) -> int:
        return int(self.meta_get("schema_version") or 0)

    def event(self, kind: str, cycle_id: str | None, payload) -> None:
        self.x("INSERT INTO events(ts,kind,cycle_id,payload_json) VALUES(?,?,?,?)", (utc_now(), kind, cycle_id, _j(payload)))

    # ------------------------------------------------------------ runs
    def run_start(self, cycle_id: str, kind: str, mode: str) -> None:
        self.x("INSERT OR REPLACE INTO runs(cycle_id,kind,mode,status,started_at) VALUES(?,?,?,?,?)",
               (cycle_id, kind, mode, "running", utc_now()))

    def run_finish(self, cycle_id: str, status: str, receipt_path: str | None, receipt: dict | None) -> None:
        self.x("UPDATE runs SET status=?, finished_at=?, receipt_path=?, receipt_json=? WHERE cycle_id=?",
               (status, utc_now(), receipt_path, _j(receipt), cycle_id))

    def last_run(self, kind: str | None = None) -> dict | None:
        if kind:
            rows = self.q("SELECT * FROM runs WHERE kind=? ORDER BY started_at DESC LIMIT 1", (kind,))
        else:
            rows = self.q("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------ tool calls
    def tool_call(self, call_id: str, cycle_id: str, tool: str, args, ok: bool, result, error: str | None) -> None:
        self.x("INSERT INTO tool_calls(id,cycle_id,ts,tool,args_json,ok,result_json,error) VALUES(?,?,?,?,?,?,?,?)",
               (call_id, cycle_id, utc_now(), tool, _j(args), 1 if ok else 0, _j(result), error))

    def tool_call_ids(self, cycle_id: str) -> set[str]:
        return {r["id"] for r in self.q("SELECT id FROM tool_calls WHERE cycle_id=? AND ok=1", (cycle_id,))}

    def tool_call_count(self, cycle_id: str) -> int:
        return int(self.q("SELECT COUNT(*) n FROM tool_calls WHERE cycle_id=?", (cycle_id,))[0]["n"])

    # ------------------------------------------------------------ proposals / gate
    def add_proposal(self, cycle_id: str, submission: int, idx: int, p: dict, valid: bool, errors: list[str]) -> int:
        return self.x(
            "INSERT INTO proposals(cycle_id,submission,idx,ts,action,pair_id,size_usd,stop_price,thesis,invalidation,"
            "evidence_json,raw_json,valid,errors_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, submission, idx, utc_now(), p.get("action"), p.get("pair_id"), _num(p.get("size_usd")),
             _num(p.get("stop_price")), p.get("thesis"), p.get("invalidation"), _j(p.get("evidence")), _j(p),
             1 if valid else 0, _j(errors)))

    def submissions(self, cycle_id: str) -> int:
        r = self.q("SELECT MAX(submission) m FROM proposals WHERE cycle_id=?", (cycle_id,))
        return int(r[0]["m"] or 0)

    def submission_count(self, cycle_id: str) -> int:
        return int(self.meta_get(f"submissions:{cycle_id}") or 0)

    def bump_submission(self, cycle_id: str) -> int:
        n = self.submission_count(cycle_id) + 1
        self.meta_set(f"submissions:{cycle_id}", str(n))
        return n

    def proposals_for(self, cycle_id: str, submission: int | None = None) -> list[dict]:
        if submission is None:
            rows = self.q("SELECT * FROM proposals WHERE cycle_id=? ORDER BY submission, idx", (cycle_id,))
        else:
            rows = self.q("SELECT * FROM proposals WHERE cycle_id=? AND submission=? ORDER BY idx", (cycle_id, submission))
        return [dict(r) for r in rows]

    def mark_final(self, cycle_id: str, submission: int) -> None:
        self.x("UPDATE proposals SET final=1 WHERE cycle_id=? AND submission=?", (cycle_id, submission))

    def add_gate(self, cycle_id: str, proposal_id: int | None, dry_run: bool, d: dict) -> int:
        return self.x(
            "INSERT INTO gate_decisions(proposal_id,cycle_id,ts,dry_run,approved,reasons_json,size_usd,limit_price,stop_price,ask)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, cycle_id, utc_now(), 1 if dry_run else 0, 1 if d.get("approved") else 0, _j(d.get("reasons")),
             _num(d.get("size_usd")), _num(d.get("limit_price")), _num(d.get("stop_price")), _num(d.get("ask"))))

    # ------------------------------------------------------------ orders / fills / closes / stops
    def add_order(self, row: dict) -> int:
        return self.x(
            "INSERT INTO orders(cycle_id,ts,venue,pair_id,side,ordertype,price,volume,oflags,timeinforce,status,txid,proposal_id,source,raw_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row.get("cycle_id"), row.get("ts") or utc_now(), row.get("venue"), row.get("pair_id"), row.get("side"),
             row.get("ordertype"), _num(row.get("price")), _num(row.get("volume")), row.get("oflags"),
             row.get("timeinforce"), row.get("status"), row.get("txid"), row.get("proposal_id"), row.get("source"),
             _j(row.get("raw"))))

    def add_fill(self, row: dict) -> int | None:
        try:
            return self.x(
                "INSERT INTO fills(ts,cycle_id,venue,pair_id,wsname,qty,cost_usd,price,fee_usd,txid,order_id,source,dedupe,raw_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["ts"], row.get("cycle_id"), row["venue"], row["pair_id"], row.get("wsname") or "", float(row["qty"]),
                 float(row["cost_usd"]), _num(row.get("price")), float(row.get("fee_usd") or 0), row.get("txid"),
                 row.get("order_id"), row.get("source") or "harness", row.get("dedupe"), _j(row.get("raw"))))
        except sqlite3.IntegrityError:
            return None

    def add_close(self, row: dict) -> int | None:
        try:
            return self.x(
                "INSERT INTO closes(ts,cycle_id,venue,pair_id,wsname,qty,proceeds_usd,price,fee_usd,rule_fired,txid,status,order_id,source,dedupe,raw_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["ts"], row.get("cycle_id"), row["venue"], row["pair_id"], row.get("wsname") or "", float(row.get("qty") or 0),
                 float(row.get("proceeds_usd") or 0), _num(row.get("price")), float(row.get("fee_usd") or 0),
                 row.get("rule_fired"), row.get("txid"), row.get("status"), row.get("order_id"),
                 row.get("source") or "harness", row.get("dedupe"), _j(row.get("raw"))))
        except sqlite3.IntegrityError:
            return None

    def fills(self, venue: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM fills WHERE venue=? ORDER BY ts, id", (venue,))]

    def closes(self, venue: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM closes WHERE venue=? ORDER BY ts, id", (venue,))]

    def set_stop(self, venue: str, pair_id: str, episode_opened_ts: str | None, stop_price: float, source: str,
                 cycle_id: str | None = None) -> int:
        return self.x("INSERT INTO stops(ts,venue,pair_id,episode_opened_ts,stop_price,source,cycle_id) VALUES(?,?,?,?,?,?,?)",
                      (utc_now(), venue, pair_id, episode_opened_ts, float(stop_price), source, cycle_id))

    def current_stop(self, venue: str, pair_id: str, episode_opened_ts: str | None) -> dict | None:
        rows = self.q("SELECT * FROM stops WHERE venue=? AND pair_id=? AND episode_opened_ts IS ? ORDER BY id DESC LIMIT 1",
                      (venue, pair_id, episode_opened_ts))
        return dict(rows[0]) if rows else None

    def first_stop(self, venue: str, pair_id: str, episode_opened_ts: str | None) -> dict | None:
        rows = self.q("SELECT * FROM stops WHERE venue=? AND pair_id=? AND episode_opened_ts IS ? ORDER BY id ASC LIMIT 1",
                      (venue, pair_id, episode_opened_ts))
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------ notes / incidents / grades
    def add_note(self, cycle_id: str, text: str, keep: int = 20) -> int:
        nid = self.x("INSERT INTO notes(ts,cycle_id,text) VALUES(?,?,?)", (utc_now(), cycle_id, text))
        self.x("DELETE FROM notes WHERE id NOT IN (SELECT id FROM notes ORDER BY id DESC LIMIT ?)", (int(keep),))
        return nid

    def notes(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (int(limit),))]

    def add_incident(self, kind: str, detail: str, check_name: str | None = None, test_name: str | None = None,
                     cycle_id: str | None = None) -> int:
        return self.x("INSERT INTO incidents(ts,kind,detail,check_name,test_name,cycle_id) VALUES(?,?,?,?,?,?)",
                      (utc_now(), kind, detail, check_name, test_name, cycle_id))

    def recent_grades(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM grades ORDER BY id DESC LIMIT ?", (int(limit),))]

    # ------------------------------------------------------------ halt
    def halt_state(self) -> dict | None:
        row = self.meta_json("halt")
        return row if row and row.get("status") == "HALTED" else None

    def set_halt(self, row: dict | None) -> None:
        self.meta_set("halt", None if row is None else json.dumps(row))


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
