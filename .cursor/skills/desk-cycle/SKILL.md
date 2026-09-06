---
name: desk-cycle
description: Run one paper cycle of the Kraken-only trading desk, then wake again in 15 minutes. Universe is online Kraken USD spot pairs. Fans SCAN then VET, SIZE on PASS/PASS_PARTIAL, paper FILLS, then RISK. BOOK is skipped. Use for /desk-cycle, run the desk, or run a paper trading cycle.
disable-model-invocation: true
---

# Desk cycle

Run one cycle of the trading desk in `~/projects/trading-desk`. Universe is Kraken USD spot. WorldMonitor is optional context only. Models come from `config.json`.

This skill is the desk. Do not run a Python cycle in place of the seats.

`config.json` must have `paper` true and `venues.fills` `"paper"`. If it does not, stop and tell the human. Do not sign AddOrder.

## Hard limits

- `venues.scan` must be `"kraken"`. `venues.fills` must be `"paper"`.
- If `ledger/halt.json` is `HALTED`, skip SIZE and FILLS. RISK may still close. Do not clear the halt. Tell the human.
- A candidate without Kraken `pair_id` is illegal. Drop it.
- Never POST private Kraken orders from a seat. Never Trading 212, FOMO, Polymarket, Kalshi, or `x.ai/bot`.
- Never read `pass` or print secrets except `scripts/kraken_balance.py`.
- Never fall back to `api.worldmonitor.app`, DexScreener, or prediction books.
- CHIEF never opens, sizes, or closes a position.
- RISK is never queued and never overruled.
- Do not launch BOOK. Prediction markets are not fillable on this desk.

## Cycle id

`cycle_id` is ISO-8601 UTC now. Pass it to every seat. Every ledger row includes `ts` and `cycle_id`.

## Steps

### 1. Universe

```bash
python3 ~/projects/trading-desk/scripts/kraken_universe.py
```

If this fails, stop. Read `ledger/universe-kraken.json`. SCAN may only rank those `pair_id`s.

### 2. WorldMonitor (optional)

```bash
~/projects/trading-desk/scripts/probe-worldmonitor.sh
```

Use `wmBase` (`http://localhost:${wmPort}`). Vite binds `[::1]`. Down or prediction-degraded is not fatal: mark SCAN `degraded` and continue. Never use prediction markets.

### 3. Fan-out seats with Task

`subagent_type` is the agent file name. `model` from `config.json` `models`.

1. SCAN (`composer-2.5`) on universe ∩ Ticker.
2. VET (`cursor-grok-4.6-high`) on SCAN's list. Nothing reaches SIZE without VET.
3. SIZE (`cursor-grok-4.6-high`) only on VET `PASS` / `PASS_PARTIAL`. Kelly clamp 6%. `PASS_PARTIAL` cuts the ticket.
4. FILLS (`composer-2.5`) only on SIZE dollars > 0. Paper ledger only. `pair_id` + `wsname`.
5. RISK (`cursor-grok-4.6-high`) on open paper fills minus closes. Always run. Kraken OHLC interval 60.

If a seat Task fails, log it, do not invent its JSON, and do not skip RISK.

### 4. Report

Read ledger files for this `cycle_id`. Every candidate must have `pair_id`. Assemble, never invent. Append `ledger/reports.jsonl`.

Empty PASS lists are valid.

### 5. Arm the 15 minute loop

After the report, keep this desk running. Follow the Cursor loop skill for a local session.

- Fixed interval: 900 seconds.
- Sentinel: `AGENT_LOOP_TICK_desk-cycle`
- Prompt: `/desk-cycle`
- Title the shell `Loop every 15m: /desk-cycle`

Check existing terminals first. If a matching loop is already running, do not start another. On the first `/desk-cycle` of a session, run the cycle now, then arm the sleeper so the next tick is 15 minutes later.

Closing this chat stops the desk.

## Ledger files

`universe-kraken.json` `halt.json` `candidates.jsonl` `rejections.jsonl` `sizes.jsonl` `fills.jsonl` `closes.jsonl` `reports.jsonl`

BOOK may write one `SKIPPED_UNIVERSE` row if invoked by mistake.
