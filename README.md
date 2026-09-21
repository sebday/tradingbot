# trading-desk

Kraken-only desk in Cursor with Grok and Composer. SCAN only ranks online Kraken USD spot pairs. No xAI Grok Bot. No WorldMonitor UI.

Sibling of `~/projects/worldmonitor` (unused as a server). This repo pulls the context feeds itself.

## What it does

CHIEF fans SCAN → VET → SIZE → FILLS, with RISK on Kraken OHLC. BOOK is skipped. A name without a Kraken `pair_id` never enters the list.

When `paper` is false, FILLS and RISK call `scripts/kraken_execute.py` for live market orders. Seats never read `pass`.

## Prerequisites

- Python 3
- Network to `api.kraken.com` public endpoints
- For live fills: `pass` entries in `config.json` `kraken.passKey` / `passSecret`
- Context: CNN Fear & Greed, alternative.me crypto F&G, Yahoo VIX, CoinPaprika (no keys)

## Universe

```bash
cd ~/projects/trading-desk
python3 scripts/kraken_universe.py
```

Writes `ledger/universe-kraken.json`: online USD/ZUSD spot pairs, no `.d` dark pools.

## Context

```bash
python3 scripts/desk_context.py
```

Writes `ledger/context.json`. SCAN may rerank existing Kraken pairs from those signals. VET uses them for STORY. Missing feeds mark SCAN `degraded`. Never `api.worldmonitor.app`.

## Run the desk

Open this folder in a Cursor agent chat and type `/desk-cycle`.

CHIEF fans the seats, then arms a 60 minute `/loop`. A second loop, `/desk-exit`, runs RISK alone every 15 minutes on Composer. That check uses the public last price. It calls Kraken private only when a close is already true.

Leave the chat open. Closing Cursor stops the desk.

Existing Kraken balances are RISK's book. `maxFillsPerCycle` is 1.

## Paper bank / live pot

`bank.startingUsd` / `bank.allocatedUsd` is the pot. Live free cash is Kraken `usd_spot`. If equivalent equity falls to 50% of allocated (`risk.haltDrawdown`), `ledger/halt.json` is written and new fills stop until you clear it.

Query-only live balance:

```bash
python3 scripts/kraken_balance.py
```

The API key must allow Create & modify orders and Cancel; never Withdraw.

## Seats

| Seat | Model | Owns |
|------|-------|------|
| CHIEF | inherit (this chat: Grok 4.7) | universe, fan-out, report |
| SCAN | composer-2.5 | Kraken ticker ranking |
| VET | inherit (this chat: Grok 4.7) | rejections |
| BOOK | unused | SKIPPED_UNIVERSE |
| SIZE | inherit (this chat: Grok 4.7) | dollars only |
| FILLS | composer-2.5 | `kraken_execute.py buy` |
| RISK | inherit (this chat: Grok 4.7) | hourly OHLC close, `kraken_execute.py sell` |
| EXIT | composer-2.5 | 15 minute RISK-only volume and trail check |

## Dry-run tests

```bash
cd ~/projects/trading-desk
python3 tests/test_dry_run.py
```

Ticket math, halt floor, universe fetch, context fetch. Tests never call AddOrder.

## Out of scope

- Pump.fun / FOMO / Polymarket / Kalshi
- Trading 212
- Printing `pass` secrets
- WorldMonitor Vite UI
- `/make-bot-ui` operator panel (later)
