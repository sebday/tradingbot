"""Seat tools. Read-only views plus submit_proposals and write_note.

This module must never import harness.broker, harness.executor, or anything that reads
`pass` (doctor checks). Every call is journaled in tool_calls with a short id the seat
cites as evidence.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config as config_mod
from .. import gatestate, paths, policy
from ..book import load_book
from ..contract import Mandate, validate_submission
from ..journal import Journal
from ..market import context as ctx
from ..market.kraken_public import market_from_env
from ..util import parse_ts, read_json

CONTEXT_PAIRS = ("XXBTZUSD", "XETHZUSD")
INTERVALS = (60, 240, 1440)


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


TOOL_SPECS = [
    {"name": "get_mandate", "description": "This cycle's contract: cycle_id, deadline, allowed actions, dollar and fill budget, "
     "stop band for new entries, observe_only flag. Call first.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_portfolio", "description": "Equity, cash, open positions with entry, qty, current stop_price, last price and P&L.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_universe", "description": "Eligible pairs for opens this cycle, ranked exactly like the mechanical shortlist "
     "(24h quote volume). Includes ask/bid/last, spread, 24h move, min ticket. Opens outside this list are rejected.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_ticker", "description": "Fresh Kraken ticker for up to 20 pairs (universe, held pairs, XXBTZUSD, XETHZUSD).",
     "inputSchema": {"type": "object", "properties": {"pair_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20}},
                     "required": ["pair_ids"]}},
    {"name": "get_candles", "description": "OHLC candles [time, open, high, low, close, vwap, volume] for one pair. "
     "interval minutes: 60, 240 or 1440. days <= 30.",
     "inputSchema": {"type": "object", "properties": {"pair_id": {"type": "string"},
                                                      "interval": {"type": "integer", "enum": list(INTERVALS)},
                                                      "days": {"type": "number", "minimum": 0.25, "maximum": 30}},
                     "required": ["pair_id"]}},
    {"name": "get_context", "description": "Market context: crypto and CNN fear & greed, VIX, large crypto moves. "
     "Includes degraded flag (degraded context halves ticket size).", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_recent_grades", "description": "How past proposals did: forward returns 1h/6h/24h, max adverse excursion, "
     "stop hits, vs BTC hold and the mechanical top-rank baseline.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}}},
    {"name": "get_notes", "description": "Your notes carried from earlier cycles (newest first).",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}}}},
    {"name": "submit_proposals", "description": "Submit this cycle's decisions. Schema-checked and dry-run through the policy gate; "
     "the response shows each proposal's gate result. You may resubmit to fix errors; at most maxSubmissions per cycle and "
     "the last valid submission is the one executed. Proposal: {action: open|close|tighten_stop|hold, pair_id, size_usd (open), "
     "stop_price (open, tighten_stop), thesis, invalidation (open), evidence: [tool_call_id, ...]}. evidence ids must be "
     "tool_call_id values returned to you this cycle.",
     "inputSchema": {"type": "object", "properties": {
         "cycle_id": {"type": "string"},
         "proposals": {"type": "array", "items": {"type": "object", "properties": {
             "action": {"type": "string", "enum": list(policy.ACTIONS)}, "pair_id": {"type": "string"},
             "size_usd": {"type": "number"}, "stop_price": {"type": "number"}, "thesis": {"type": "string"},
             "invalidation": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
             "required": ["action", "thesis"]}}},
         "required": ["cycle_id", "proposals"]}},
    {"name": "write_note", "description": "Save a short note for future cycles (size-capped). Use for lessons, not chatter.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]


class DeskTools:
    def __init__(self, cfg: dict | None = None, journal: Journal | None = None, market=None):
        self.cfg = cfg or config_mod.load()
        self.journal = journal or Journal()
        self.market = market or market_from_env(self.cfg)
        self._n = 0

    # -------------------------------------------------------------- plumbing
    def active(self) -> tuple[Mandate, dict]:
        act = read_json(paths.active_cycle_path())
        if not act or not act.get("mandate_path"):
            raise ToolError("NO_ACTIVE_CYCLE", "no active desk cycle")
        m = read_json(Path(act["mandate_path"]))
        if not m:
            raise ToolError("NO_MANDATE", "mandate file missing")
        return Mandate.from_dict(m), act

    def call(self, name: str, args: dict | None) -> dict:
        args = args or {}
        try:
            mandate, _ = self.active()
        except ToolError as e:
            return {"ok": False, "error": {"code": e.code, "message": e.message}}
        cycle_id = mandate.cycle_id
        compact = cycle_id.replace("-", "").replace(":", "")
        n = self.journal.tool_call_count(cycle_id) + 1
        call_id = f"t{compact}-{n:02d}"
        max_calls = int(((self.cfg.get("harness") or {}).get("seat") or {}).get("maxToolCalls", 80))
        try:
            if n > max_calls:
                raise ToolError("TOOL_BUDGET", f"tool call budget {max_calls} exhausted; submit or stop")
            if time.time() > (parse_ts(mandate.deadline) or 0):
                raise ToolError("DEADLINE", "cycle deadline passed; nothing more will be accepted")
            fn = getattr(self, "t_" + name, None)
            if fn is None:
                raise ToolError("UNKNOWN_TOOL", f"unknown tool {name}")
            data = fn(mandate, args, call_id)
            result = {"ok": True, "tool_call_id": call_id, "data": data}
            self.journal.tool_call(call_id, cycle_id, name, args, True, _trim(data), None)
            return result
        except ToolError as e:
            err = {"code": e.code, "message": e.message}
            self.journal.tool_call(call_id, cycle_id, name, args, False, None, json.dumps(err))
            return {"ok": False, "tool_call_id": call_id, "error": err}
        except Exception as e:  # noqa: BLE001
            err = {"code": "INTERNAL", "message": f"{type(e).__name__}: {e}"[:400]}
            self.journal.tool_call(call_id, cycle_id, name, args, False, None, json.dumps(err))
            return {"ok": False, "tool_call_id": call_id, "error": err}

    # -------------------------------------------------------------- reads
    def t_get_mandate(self, m: Mandate, args: dict, call_id: str) -> dict:
        d = m.to_dict()
        d.pop("universe", None)
        d["universe_count"] = len(m.universe)
        d["submissions_used"] = self.journal.submission_count(m.cycle_id)
        return d

    def t_get_portfolio(self, m: Mandate, args: dict, call_id: str) -> dict:
        book = load_book(self.journal, m.venue)
        pids = [p.pair_id for p in book.open_positions()]
        ticks = self.market.tickers(pids) if pids else {}
        rows = []
        for p in book.open_positions():
            last = (ticks.get(p.pair_id) or {}).get("last")
            rows.append({"pair_id": p.pair_id, "wsname": p.wsname, "qty": p.qty, "entry": p.entry,
                         "cost_usd": round(p.cost_usd, 2), "opened_ts": p.opened_ts, "stop_price": p.stop_price,
                         "stop_source": p.stop_source, "last": last,
                         "pnl_usd": round((last - p.entry) * p.qty, 2) if last else None,
                         "pct_vs_entry": round((last / p.entry - 1) * 100, 2) if last and p.entry else None})
        port = dict(m.portfolio)
        port["positions"] = rows
        port["open_count"] = len(rows)
        port["max_open"] = int(self.cfg.get("maxOpenBooks") or 7)
        return port

    def t_get_universe(self, m: Mandate, args: dict, call_id: str) -> list:
        keep = ("rank", "pair_id", "wsname", "last", "ask", "bid", "spread", "pct_change", "high_24h", "low_24h",
                "volume_24h_quote", "trades_24h", "min_ticket_usd")
        return [{k: r.get(k) for k in keep} for r in m.universe]

    def _allowed_pairs(self, m: Mandate) -> set:
        book = load_book(self.journal, m.venue)
        return {r["pair_id"] for r in m.universe} | set(book.positions) | set(CONTEXT_PAIRS)

    def t_get_ticker(self, m: Mandate, args: dict, call_id: str) -> dict:
        pids = args.get("pair_ids")
        if not isinstance(pids, list) or not pids or len(pids) > 20:
            raise ToolError("BAD_ARGS", "pair_ids must be a list of 1..20 pair ids")
        allowed = self._allowed_pairs(m)
        bad = [p for p in pids if p not in allowed]
        if bad:
            raise ToolError("PAIR_NOT_ALLOWED", f"not in universe/held/context pairs: {bad}")
        t = self.market.tickers([str(p) for p in pids])
        keep = ("ask", "bid", "last", "spread", "pct_change", "high_24h", "low_24h", "volume_24h_quote")
        return {pid: {k: v.get(k) for k in keep} for pid, v in t.items()}

    def t_get_candles(self, m: Mandate, args: dict, call_id: str) -> dict:
        pid = args.get("pair_id")
        if pid not in self._allowed_pairs(m):
            raise ToolError("PAIR_NOT_ALLOWED", f"{pid} not in universe/held/context pairs")
        interval = int(args.get("interval") or 60)
        if interval not in INTERVALS:
            raise ToolError("BAD_ARGS", f"interval must be one of {INTERVALS}")
        days = float(args.get("days") or 2)
        if not 0.25 <= days <= 30:
            raise ToolError("BAD_ARGS", "days must be between 0.25 and 30")
        bars = self.market.ohlc(pid, interval)
        n = max(1, int(days * 1440 / interval))
        out = [[int(float(c[0])), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]), float(c[6])]
               for c in bars[-n:]]
        return {"pair_id": pid, "interval": interval, "count": len(out), "candles": out}

    def t_get_context(self, m: Mandate, args: dict, call_id: str) -> dict:
        c = ctx.load()
        if not c:
            raise ToolError("NO_CONTEXT", "context unavailable")
        return {k: c.get(k) for k in ("fetchedAt", "degraded", "unavailable", "errors", "signals", "cryptoFearGreed",
                                      "cnn", "vix")} | {"quotes": [{k: q.get(k) for k in ("symbol", "price", "change", "change7d")}
                                                                   for q in c.get("quotes") or []]}

    def t_get_recent_grades(self, m: Mandate, args: dict, call_id: str) -> list:
        limit = int(args.get("limit") or 20)
        keep = ("ts", "cycle_id", "pair_id", "action", "approved", "ret_1h", "ret_6h", "ret_24h", "mae_24h", "stop_hit",
                "btc_24h", "baseline_pair", "baseline_24h")
        return [{k: g.get(k) for k in keep} for g in self.journal.recent_grades(min(limit, 50))]

    def t_get_notes(self, m: Mandate, args: dict, call_id: str) -> list:
        limit = int(args.get("limit") or 10)
        return [{"ts": n["ts"], "cycle_id": n["cycle_id"], "text": n["text"]} for n in self.journal.notes(min(limit, 20))]

    # -------------------------------------------------------------- writes
    def t_write_note(self, m: Mandate, args: dict, call_id: str) -> dict:
        text = args.get("text")
        seat = (self.cfg.get("harness") or {}).get("seat") or {}
        cap = int(seat.get("noteMaxChars", 2000))
        if not isinstance(text, str) or not text.strip():
            raise ToolError("BAD_ARGS", "text required")
        if len(text) > cap:
            raise ToolError("NOTE_TOO_LONG", f"note over {cap} chars")
        nid = self.journal.add_note(m.cycle_id, text.strip(), keep=int(seat.get("notesKept", 20)))
        return {"note_id": nid}

    def t_submit_proposals(self, m: Mandate, args: dict, call_id: str) -> dict:
        max_sub = int(m.max_submissions)
        used = self.journal.submission_count(m.cycle_id)
        if used >= max_sub:
            raise ToolError("SUBMISSION_LIMIT", f"all {max_sub} submissions used; the last valid one stands")
        sub = self.journal.bump_submission(m.cycle_id)
        ids = self.journal.tool_call_ids(m.cycle_id)
        proposals, per, top = validate_submission(args, m, ids)
        all_valid = not top and all(not e for e in per)
        pids = []
        for i, (p, errs) in enumerate(zip(proposals, per)):
            pids.append(self.journal.add_proposal(m.cycle_id, sub, i, p, not errs and not top, errs + top))
        results = []
        if all_valid:
            fresh = {}
            try:
                want = sorted({p.get("pair_id") for p in proposals if p.get("pair_id")})
                fresh = self.market.tickers(want) if want else {}
            except Exception:  # noqa: BLE001
                fresh = {}
            state = gatestate.build(self.cfg, self.journal, m, tickers=fresh)
            decisions = policy.gate(proposals, state)
            for pid_row, d in zip(pids, decisions):
                self.journal.add_gate(m.cycle_id, pid_row, True, d)
                results.append({"pair_id": d["pair_id"], "action": d["action"], "would_approve": d["approved"],
                                "reasons": [{"rule": r, "text": policy.RULES.get(r, r)} for r in d["reasons"]],
                                "size_usd": d.get("size_usd"), "stop_price": d.get("stop_price")})
        return {"submission": sub, "submissions_left": max_sub - sub, "valid": all_valid,
                "errors": top, "proposal_errors": [{"index": i, "errors": e} for i, e in enumerate(per) if e],
                "dry_run_gate": results,
                "note": "valid submissions replace earlier ones; the gate re-runs with fresh prices at execution"}


def _trim(data, limit: int = 20000):
    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return data
    return {"truncated": True, "chars": len(text), "head": text[:limit]}
