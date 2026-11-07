"""harness doctor: the harness checklist with an evidence-backed yes/no per item."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from . import config as config_mod, paths, policy, sensors
from .book import load_book
from .journal import SCHEMA_VERSION
from .util import parse_ts, read_json

HARNESS = paths.ROOT / "harness"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            base = "." * node.level + mod
            out.add(base)
            out.update(f"{base}.{a.name}" if mod else f"{'.' * node.level}{a.name}" for a in node.names)
    return out


def _py_files() -> list[Path]:
    return sorted(p for p in HARNESS.rglob("*.py") if "__pycache__" not in p.parts)


def check_side_effects() -> tuple[bool, dict]:
    offenders = {"kraken_private_importers": [], "addorder_strings": [], "pass_calls": [], "seat_bad_imports": [],
                 "allow_orders_true": []}
    for p in _py_files():
        rel = str(p.relative_to(paths.ROOT))
        if rel == "harness/doctor.py":
            continue
        text = p.read_text(encoding="utf-8")
        imps = _imports(p)
        if any("kraken_private" in i for i in imps) and rel != "harness/broker/kraken.py":
            offenders["kraken_private_importers"].append(rel)
        if '"AddOrder"' in text and rel != "harness/broker/kraken_private.py":
            offenders["addorder_strings"].append(rel)
        if '["pass", "show"' in text and rel != "harness/broker/kraken_private.py":
            offenders["pass_calls"].append(rel)
        if rel.startswith("harness/seat/"):
            bad = [i for i in imps if any(x in i for x in ("broker", "executor", "legacy", "kraken_private", "subprocess"))
                   and not (rel == "harness/seat/runner.py" and i == "subprocess")]
            if bad:
                offenders["seat_bad_imports"].append({rel: sorted(bad)})
        if "allow_orders=True" in text and rel not in ("harness/__main__.py",):
            offenders["allow_orders_true"].append(rel)
    ok = not any(offenders.values())
    return ok, offenders


def check_rules_dual_written() -> tuple[bool, dict]:
    agents = (paths.seat_dir() / "AGENTS.md")
    a_text = agents.read_text(encoding="utf-8") if agents.exists() else ""
    tests_text = "\n".join(p.read_text(encoding="utf-8") for p in (paths.ROOT / "tests").glob("test_harness_*.py"))
    missing_agents = [r for r in policy.RULES if f"[{r}]" not in a_text]
    missing_tests = [r for r in policy.RULES if f"def test_rule_{r.lower()}" not in tests_text]
    return not missing_agents and not missing_tests, {"rules": len(policy.RULES), "missing_in_agents_md": missing_agents,
                                                        "missing_tests": missing_tests}


def check_tests() -> tuple[bool, dict]:
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_harness_*.py"],
                          cwd=str(paths.ROOT), capture_output=True, text=True, timeout=600)
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    return proc.returncode == 0, {"exit_code": proc.returncode, "tail": tail}


def systemctl(*args) -> str:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def run_doctor(cfg: dict, journal, skip_tests: bool = False) -> list[dict]:
    items: list[dict] = []

    def add(item: str, ok, evidence):
        items.append({"item": item, "ok": ok, "evidence": evidence})

    add("config valid", True, {"hash": config_mod.config_hash(cfg)[:16]})
    add("harness mode is paper", config_mod.mode(cfg) == "paper", {"harness.mode": config_mod.mode(cfg),
                                                                   "legacy paper flag": cfg.get("paper")})
    add("live.buyOflags is fciq", (cfg.get("live") or {}).get("buyOflags", "fciq") == "fciq",
        {"buyOflags": (cfg.get("live") or {}).get("buyOflags")})
    add("journal schema current", journal.schema_version() == SCHEMA_VERSION,
        {"db": str(journal.path), "schema_version": journal.schema_version()})
    ok, ev = check_side_effects()
    add("every side effect gated (only broker/kraken reads pass or calls AddOrder; seat imports no hands)", ok, ev)
    ok, ev = sensors.seat_isolation()
    gm = Path.home() / ".cursor" / "mcp.json"
    global_servers = sorted(((read_json(gm, {}) or {}).get("mcpServers") or {}).keys()) if gm.exists() else []
    ev["global_mcp_servers"] = global_servers
    add("seat isolated (deny Shell(*)/Write(**), only desk MCP, no global MCP servers)", ok and not global_servers, ev)
    ok, ev = check_rules_dual_written()
    add("hard rules written twice (AGENTS.md line + test)", ok, ev)

    runs = journal.q("SELECT cycle_id, status, receipt_path FROM runs WHERE kind='cycle'")
    missing = [r["cycle_id"] for r in runs if not r["receipt_path"] or not Path(r["receipt_path"]).exists()]
    add("every cycle run left a receipt", not missing, {"cycles": len(runs), "missing": missing[:10]})

    li = journal.meta_json("legacy_import")
    add("legacy book imported and matches desk_book", bool(li) and li.get("legacy_crosscheck") == "ok", li or "never run")

    book = load_book(journal, "kraken")
    stop_rows = []
    bad = []
    for p in book.open_positions():
        first = journal.first_stop("kraken", p.pair_id, p.opened_ts)
        expect = policy.legacy_stop_price(p.entry, cfg)
        hist = [r["stop_price"] for r in journal.q("SELECT stop_price FROM stops WHERE venue='kraken' AND pair_id=? AND "
                                                    "episode_opened_ts IS ? ORDER BY id", (p.pair_id, p.opened_ts))]
        monotonic = all(b >= a for a, b in zip(hist, hist[1:]))
        row = {"pair_id": p.pair_id, "stop": p.stop_price, "legacy_formula": round(expect, 10),
               "source": (first or {}).get("source"), "monotonic": monotonic}
        stop_rows.append(row)
        if first is None or not monotonic or (first["source"] == "legacy_import"
                                              and abs(first["stop_price"] - expect) > max(1e-9, expect * 1e-9)):
            bad.append(p.pair_id)
    add("live stops frozen from legacy (not recomputed) and never loosened", bool(stop_rows) and not bad,
        {"open": len(stop_rows), "bad": bad, "stops": stop_rows})

    rec = journal.meta_json("reconcile:kraken")
    add("reconcile vs Kraken matches", bool(rec) and bool(rec.get("clean")),
        {k: (rec or {}).get(k) for k in ("ts", "missing", "untracked", "unmapped")} if rec else "never reconciled "
        "(run: python3 -m harness import-legacy --kraken)")

    halt = sensors.is_halted(journal)
    add("desk not halted", halt is None, halt or "clear")

    old_desk = systemctl("is-enabled", "evo-trading-desk.timer")
    old_exit = systemctl("is-active", "evo-trading-exit.timer")
    new_cycle = systemctl("is-enabled", "desk-cycle.timer")
    add("legacy entry timer disabled", old_desk != "enabled", {"evo-trading-desk.timer": old_desk})
    add("legacy exit timer still protecting live positions", old_exit == "active" or not book.positions,
        {"evo-trading-exit.timer": old_exit, "live_open": len(book.positions)})
    add("new cycle timer not enabled while in paper", new_cycle != "enabled" or config_mod.mode(cfg) == "paper",
        {"desk-cycle.timer": new_cycle})

    pbook = load_book(journal, "paper")
    last_exits = journal.last_run("exits")
    add("paper exits running when paper positions are open", not pbook.positions or bool(last_exits),
        {"paper_open": len(pbook.positions), "last_exits": (last_exits or {}).get("started_at")})

    last = journal.last_run("cycle")
    add("last cycle receipt", bool(last), {k: (last or {}).get(k) for k in ("cycle_id", "status", "finished_at")}
        if last else "no cycles yet")

    if skip_tests:
        add("tests pass", None, "skipped (--skip-tests)")
    else:
        ok, ev = check_tests()
        add("tests pass", ok, ev)
    return items


def render(items: list[dict]) -> str:
    lines = []
    for it in items:
        mark = "SKIP" if it["ok"] is None else ("YES " if it["ok"] else "NO  ")
        ev = it["evidence"]
        ev_s = ev if isinstance(ev, str) else json.dumps(ev, default=str)
        if len(ev_s) > 300:
            ev_s = ev_s[:297] + "..."
        lines.append(f"[{mark}] {it['item']}\n        {ev_s}")
    n_no = sum(1 for it in items if it["ok"] is False)
    lines.append(f"\n{len(items) - n_no}/{len(items)} ok" + (f", {n_no} failing" if n_no else ""))
    return "\n".join(lines)
