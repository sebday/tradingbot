"""The contract between harness and seat: a Mandate in, proposals out.

validate_submission() is schema only (types, enums, sizes, evidence ids). The policy
gate decides whether a valid proposal may trade.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import HARNESS_VERSION
from .policy import ACTIONS
from .util import sha256_obj

MAX_PROPOSALS = 12
MAX_THESIS = 1200
MAX_INVALIDATION = 600


@dataclass
class Mandate:
    cycle_id: str
    mode: str
    venue: str
    created_at: str
    deadline: str
    allowed_actions: list
    budget_usd: float
    max_fills: int
    max_submissions: int
    universe: list               # ranked candidate rows (same rank as legacy shortlist)
    universe_hash: str
    portfolio: dict              # {equity_usd, usd_spot, invested_usd, positions:[...], halted}
    observe_only: bool
    observe_reasons: list = field(default_factory=list)
    harness_version: str = HARNESS_VERSION
    config_hash: str = ""
    stop_band: dict = field(default_factory=dict)  # {minStopPct, maxStopPct}
    ticket_cap_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Mandate":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: d[k] for k in keys if k in d})


def universe_hash(universe: list[dict]) -> str:
    return sha256_obj(sorted(r["pair_id"] for r in universe))


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_proposal(p, mandate: Mandate, tool_call_ids: set) -> list[str]:
    errs: list[str] = []
    if not isinstance(p, dict):
        return ["proposal must be an object"]
    action = p.get("action")
    if action not in ACTIONS:
        errs.append(f"action must be one of {list(ACTIONS)}")
        return errs
    pid = p.get("pair_id")
    if action != "hold" and (not isinstance(pid, str) or not pid):
        errs.append("pair_id required")
    if action == "open":
        if not _num(p.get("size_usd")) or float(p["size_usd"]) <= 0:
            errs.append("size_usd must be a positive number for open")
        if not _num(p.get("stop_price")) or float(p["stop_price"]) <= 0:
            errs.append("stop_price must be a positive number for open")
        inv = p.get("invalidation")
        if not isinstance(inv, str) or not inv.strip():
            errs.append("invalidation required for open")
        elif len(inv) > MAX_INVALIDATION:
            errs.append(f"invalidation over {MAX_INVALIDATION} chars")
    if action == "tighten_stop":
        if not _num(p.get("stop_price")) or float(p["stop_price"]) <= 0:
            errs.append("stop_price must be a positive number for tighten_stop")
    thesis = p.get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        errs.append("thesis required")
    elif len(thesis) > MAX_THESIS:
        errs.append(f"thesis over {MAX_THESIS} chars")
    ev = p.get("evidence")
    if action != "hold":
        if not isinstance(ev, list) or not ev:
            errs.append("evidence must be a non-empty list of tool_call ids from this cycle")
        else:
            bad = [e for e in ev if not isinstance(e, str) or e not in tool_call_ids]
            if bad:
                errs.append(f"unknown evidence ids (not tool calls from this cycle): {bad[:5]}")
    elif ev is not None and not isinstance(ev, list):
        errs.append("evidence must be a list")
    extra = set(p) - {"action", "pair_id", "size_usd", "stop_price", "thesis", "invalidation", "evidence"}
    if extra:
        errs.append(f"unknown fields: {sorted(extra)}")
    return errs


def validate_submission(payload, mandate: Mandate, tool_call_ids: set) -> tuple[list[dict], list[list[str]], list[str]]:
    """-> (proposals, per-proposal errors, submission-level errors)."""
    top: list[str] = []
    if not isinstance(payload, dict):
        return [], [], ["submission must be an object {cycle_id, proposals:[...]}"]
    if payload.get("cycle_id") != mandate.cycle_id:
        top.append(f"cycle_id must be {mandate.cycle_id!r}")
    props = payload.get("proposals")
    if not isinstance(props, list):
        return [], [], top + ["proposals must be a list"]
    if len(props) > MAX_PROPOSALS:
        top.append(f"at most {MAX_PROPOSALS} proposals")
    per = [validate_proposal(p, mandate, tool_call_ids) for p in props]
    return [p if isinstance(p, dict) else {} for p in props], per, top
