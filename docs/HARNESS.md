# Desk harness

Brain, hands and history kept apart.

- **Brain**: the trader seat. `cursor-agent -p` in `seat/` with only the `desk` MCP server
  (`harness/seat/mcp_server.py`). `seat/.cursor/cli.json` denies `Shell(*)`, `Write(**)`,
  `Delete(**)`, `WebFetch(*)`. The seat's env is scrubbed; it holds no keys.
- **Hands**: `harness/executor.py` + `harness/broker/{paper,kraken}.py`. Only
  `harness/broker/kraken_private.py` reads `pass`; only `harness/__main__.py` builds a Kraken
  broker with orders allowed, and only when `harness.mode == "live"`.
- **History**: `state/desk.db` (SQLite journal) and `runs/<cycle_id>/` (mandate, sensors,
  transcript, proposals, gate_state, gate, orders, receipt).

## Cycle

```
sensors.preflight -> red?  observe-only receipt (seat not run)
                  -> green: build Mandate (universe = legacy shortlist rank)
                  -> seat (MCP tools, max 3 submissions, one deadline)
                  -> last valid submission -> policy.gate (fresh prices) -> executor -> receipt
```

Timeout, no valid submission, or any exception => no trade, receipt still written.

## Stops

- Legacy positions: `import-legacy` freezes `entry * (1 - risk.stopLossPct)` once per episode
  (`stops.source = legacy_import`). Re-imports never overwrite. Exits use the stored stop.
- New entries: model stop must sit between `harness.entry.minStopPct` and `maxStopPct` under
  the ask. `tighten_stop` can only raise a stop.

## Commands

```bash
cd ~/projects/tradingbot
python3 -m harness import-legacy --dry-run     # preview: open positions + frozen stops
python3 -m harness import-legacy --kraken      # write journal, reconcile vs Kraken (query-only)
python3 -m harness doctor                      # checklist (add --skip-tests for speed)
python3 -m harness status --pretty             # bar JSON (legacy keys + harness/last_receipt/gate)
python3 -m harness cycle --seat hold           # paper cycle with the scripted hold seat (no LLM)
python3 -m harness cycle --seat baseline       # paper cycle, mechanical baseline seat
python3 -m harness cycle                       # paper cycle with the real cursor-agent seat
python3 -m harness exits                       # paper exits; --venue kraken = shadow vs legacy
python3 -m harness eval                        # grade proposals (24h+ old)
python3 -m harness replay <cycle_id>           # re-run current policy on a stored trace
python3 -m harness halt "reason" / resume
python3 -m harness receipts -n 5
python3 -m unittest discover -s tests -p 'test_harness_*.py'
```

`bin/desk <cmd>` is the same CLI under `python3 -I`.

## Timers (new names; not enabled)

`systemd/desk-cycle.{service,timer}` (hourly :02), `desk-exits` (every 5 min),
`desk-eval` (daily 06:30). `evo-trading-exit.timer` keeps guarding the live book until cutover.

## Cutover (by hand, in order)

0. Done: `evo-trading-desk.timer` disabled; snapshot taken.
1. Paper soak: link + enable `desk-exits.timer` and `desk-cycle.timer` while `harness.mode`
   is `"paper"` (paper book only; Kraken exits stay shadow). Watch `doctor`, receipts, and
   `exits --venue kraken` shadow for `legacy_disagreements` (must stay empty).
2. Exits: set `harness.exits.liveKraken: true`, then stop/disable `evo-trading-exit.timer`
   in the same minute. `desk-exits` must then run with `--venue kraken`
   (edit `desk-exits.service` ExecStart) so it sells live positions.
3. Entries: after enough clean paper cycles, set `harness.mode: "live"`.
4. Move old `scripts/`, `bin/run-desk-*`, `.cursor/skills/desk-*`, `evo-*`/`omarchy-*` units
   to `legacy/`, then delete. Point `bin/trading-status` at `bin/desk status`.
