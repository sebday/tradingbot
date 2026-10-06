---
name: desk-cycle
description: Run one live Kraken cycle of the trading desk, then wake again in 60 minutes. Universe is online Kraken USD spot pairs. Fans SCAN then VET, SIZE on PASS/PASS_PARTIAL, FILLS via kraken_execute, then RISK. BOOK is skipped. Use for /desk-cycle, run the desk, or run a trading cycle.
disable-model-invocation: true
---

# Desk cycle

Run one cycle of the trading desk in `~/projects/trading-bot`. Universe is Kraken USD spot. Models come from `config.json`.

This skill is the desk. Do not run a Python cycle in place of the seats.

`config.json` `venues.scan` is `"kraken"`. If `paper` is false and `venues.fills` is `"kraken"`, this is live. Seats still never read `pass`. Live `AddOrder` is only `scripts/kraken_execute.py`.

If `paper` is true, FILLS writes ledger rows and does not call execute.

## Hard limits

- `venues.scan` must be `"kraken"`.
- If `ledger/halt.json` is `HALTED`, skip SIZE and FILLS. RISK may still close. Do not clear the halt. Tell the human.
- A candidate without Kraken `pair_id` is illegal. Drop it.
- Never POST private Kraken from a seat. FILLS and RISK call `scripts/kraken_execute.py`. Never Trading 212, FOMO, Polymarket, Kalshi, or `x.ai/bot`.
- Never read `pass` or print secrets except `scripts/kraken_balance.py` / `scripts/kraken_execute.py`.
- Never fall back to `api.worldmonitor.app`, DexScreener, or prediction books.
- Never require the WorldMonitor UI. Context is `scripts/desk_context.py` → `ledger/context.json`.
- CHIEF never opens, sizes, or closes a position.
- RISK is never queued and never overruled.
- Do not launch BOOK. Prediction markets are not fillable on this desk.
- Existing Kraken inventory is the desk's book. RISK owns it.

## Cycle id

`cycle_id` is ISO-8601 UTC now. Pass it to every seat. Every ledger row includes `ts` and `cycle_id`.

## Steps

### 1. Universe

```bash
python3 ~/projects/trading-bot/scripts/kraken_universe.py
```

If this fails, stop. Read `ledger/universe-kraken.json`. SCAN may only rank those `pair_id`s.

### 2. Context

```bash
python3 ~/projects/trading-bot/scripts/desk_context.py
```

Writes `ledger/context.json` (CNN Fear & Greed, crypto F&G, Yahoo VIX, CoinPaprika). A missing Yahoo VIX stays in `errors` and does not set `degraded`. CNN, crypto F&G, or CoinPaprika missing still sets `degraded`. Unavailable, or a missing file, marks SCAN `degraded` and the cycle continues. Never use prediction markets. Never call `api.worldmonitor.app`.

If live, also:

```bash
python3 ~/projects/trading-bot/scripts/kraken_balance.py
python3 ~/projects/trading-bot/scripts/kraken_execute.py opens
```

Pass `usd_spot` / leftover assets / open pair_ids to SIZE and RISK.

### 3. Fan-out seats with Task

`subagent_type` is the agent file name. `model` from `config.json` `models`.

1. SCAN (`composer-2.5`) on universe ∩ Ticker. Context from `ledger/context.json`.
2. VET (`inherit`) on SCAN's list. Nothing reaches SIZE without VET. STORY uses `ledger/context.json` signals.
3. SIZE (`inherit`) only on VET `PASS` / `PASS_PARTIAL`. Kelly clamp 6% of live free cash (`usd_spot`). `PASS_PARTIAL` cuts the ticket. Live `maxFillsPerCycle` is 1. `maxOpenBooks` counts open Kraken positions.
4. FILLS (`composer-2.5`) only on SIZE dollars > 0. Live: `python3 scripts/kraken_execute.py buy ...`. Never sign in the seat.
5. RISK (`inherit`) on open Kraken fills minus closes, plus leftover non-USD Kraken balances. Always run. Close via `python3 scripts/kraken_execute.py sell ...`. Kraken OHLC interval 60.

`inherit` means omit the Task `model` argument. The seat then runs on this chat's model: Grok 4.7, normal speed, not Fast. Do not pass `cursor-grok-4.6-high` or a Fast slug. SCAN and FILLS stay `composer-2.5`.

If a seat Task fails, log it, do not invent its JSON, and do not skip RISK.

### 4. Report

Read ledger files for this `cycle_id`. Every candidate must have `pair_id`. Assemble, never invent. Append `ledger/reports.jsonl`.

Empty PASS lists are valid.

### 5. Scheduled loop (systemd)

Hourly automation is **not** a Cursor chat tab. The `evo.trading` plugin ships:

- `bin/run-desk-cycle` — one `cursor-agent -p "/desk-cycle"` with flock
- `systemd --user` timer `omarchy-trading-desk.timer` (every 3600s, on boot)

After the report, do **not** arm `AGENT_LOOP_TICK_desk-cycle`. Do not start a bash sleep loop in a terminal.

If the timer is not enabled, tell the human:

```bash
systemctl --user link ~/projects/trading-bot/systemd/omarchy-trading-desk.{service,timer}
systemctl --user enable --now omarchy-trading-desk.timer
```

Do not arm a bash sleep loop. Open positions are also checked by `evo-trading-exit.timer` (`scripts/desk_exit_check.py`, every 5 minutes, public prices, then `kraken_execute.py sell`). Hourly RISK uses the same close rules. Do not disable that timer.

## Ledger files

`universe-kraken.json` `context.json` `halt.json` `candidates.jsonl` `rejections.jsonl` `sizes.jsonl` `fills.jsonl` `closes.jsonl` `reports.jsonl`

BOOK may write one `SKIPPED_UNIVERSE` row if invoked by mistake.
