---
name: desk-exit
description: Optional one-off RISK-only exit check (public ticker and OHLC until a close is true). The live desk does not run a separate loop; use /desk-cycle hourly RISK instead. Use for manual /desk-exit only.
disable-model-invocation: true
---

# Desk exit (manual)

Optional manual check. The scheduled exit is `evo-trading-exit.timer` → `scripts/desk_exit_check.py` every 5 minutes. `/desk-cycle` RISK uses the same rules once an hour. Run this skill only when a human wants an extra look.

Run this skill only when a human explicitly asks for an extra exit check between hours.

No universe refresh. No context. No SCAN, VET, SIZE, or FILLS. No buys. BOOK stays unused.

`models.exit` is `composer-2.5` if you Task RISK for this path.

Do not arm a sleeper loop. Do not start `AGENT_LOOP_TICK_desk-exit`.

## Hard limits

- Do not call `kraken_balance.py` or `kraken_execute.py opens` on the way in.
- Do not write `ledger/halt.json` here. The hourly cycle owns the pot halt.
- Do not clear a halt. If `ledger/halt.json` is `HALTED`, still run this check. RISK may close.
- Never POST private Kraken except `scripts/kraken_execute.py sell` after a rule has already fired and one balance read has confirmed the qty.
- Temporary lockout: do not retry. Do not sell.
- CHIEF never sells. RISK does.

## Steps

### 1. Book

Use the last known live book: pair_id, wsname, qty, entry (filled USD / base qty), filled_usd. Dust under about $1 stays skipped. Pass that to RISK. Do not refresh it from Kraken yet.

`cycle_id` is ISO-8601 UTC now.

### 2. RISK

Task `generalPurpose`, model `composer-2.5`. Follow `~/projects/trading-bot/.cursor/agents/risk.md` EXIT CHECK.

Public OHLC interval 60 and the public ticker last. A trail-armed winner closes only on TRAIL_PEAK. VOLUME_6H closes only while the peak is still under +30%. One full sell, never a trim.

If neither rule fires, RISK stops. No private call.

If one fires, RISK calls `kraken_balance.py` once, then:

```bash
python3 ~/projects/trading-bot/scripts/kraken_execute.py sell \
  --pair <pair_id> --wsname <wsname> --cycle-id <cycle_id> \
  --rule VOLUME_6H --volume-base <qty>
```

Use `--rule TRAIL_PEAK` for the giveback close.

### 3. Report

Append `ledger/reports.jsonl` with `kind` `exit`. Assemble, never invent. `scan` 0, `vet_pass` 0, `fills` 0. Record holds, closes, ratios, peaks, floors, and whether a private call happened.
