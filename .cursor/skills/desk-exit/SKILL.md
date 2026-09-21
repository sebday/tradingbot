---
name: desk-exit
description: Run one RISK-only exit check on the live Kraken book, then wake again in 15 minutes. Public ticker and OHLC only, until a close is already true. Use for /desk-exit.
disable-model-invocation: true
---

# Desk exit

Run one exit check of the trading desk in `~/projects/trading-desk`. This is not a desk cycle.

No universe refresh. No context. No SCAN, VET, SIZE, or FILLS. No buys. BOOK stays unused.

`config.json` `risk.exitPollSeconds` is 900. `models.exit` is `composer-2.5`.

If a `/desk-cycle` is already running in this turn, skip this check. That cycle's RISK covers the book.

## Hard limits

- Do not call `kraken_balance.py` or `kraken_execute.py opens` on the way in.
- Do not write `ledger/halt.json` here. The hourly cycle owns the pot halt.
- Do not clear a halt. If `ledger/halt.json` is `HALTED`, still run this check. RISK may close.
- Never POST private Kraken except `scripts/kraken_execute.py sell` after a rule has already fired and one balance read has confirmed the qty.
- Temporary lockout: do not retry. Do not sell.
- CHIEF never sells. RISK does.
- Do not start a second 15 minute loop. Do not stop the 60 minute `/desk-cycle` loop.

## Steps

### 1. Book

Use the last known live book: pair_id, wsname, qty, entry (filled USD / base qty), filled_usd. Dust under about $1 stays skipped. Pass that to RISK. Do not refresh it from Kraken yet.

`cycle_id` is ISO-8601 UTC now.

### 2. RISK

Task `generalPurpose`, model `composer-2.5`. Follow `~/projects/trading-desk/.cursor/agents/risk.md` EXIT CHECK.

Public OHLC interval 60 and the public ticker last. Volume rule first, then TRAIL_PEAK. One full sell, never a trim.

If neither rule fires, RISK stops. No private call.

If one fires, RISK calls `kraken_balance.py` once, then:

```bash
python3 ~/projects/trading-desk/scripts/kraken_execute.py sell \
  --pair <pair_id> --wsname <wsname> --cycle-id <cycle_id> \
  --rule VOLUME_6H --volume-base <qty>
```

Use `--rule TRAIL_PEAK` for the giveback close.

### 3. Report

Append `ledger/reports.jsonl` with `kind` `exit`. Assemble, never invent. `scan` 0, `vet_pass` 0, `fills` 0. Record holds, closes, ratios, peaks, floors, and whether a private call happened.

### 4. Arm the 15 minute loop

Follow the Cursor loop skill for a local session.

- Fixed interval: 900 seconds.
- Sentinel: `AGENT_LOOP_TICK_desk-exit`
- Prompt: `/desk-exit`
- Title the shell `Loop every 15m: /desk-exit`

Check existing terminals first. If that loop is already running, do not start another. On the first `/desk-exit` of a session, run the check now, then arm the sleeper so the next tick is 15 minutes later.

Closing this chat stops the check.
