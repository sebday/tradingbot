# trading-desk

Kraken-only paper desk in Cursor with Grok and Composer. SCAN only ranks online Kraken USD spot pairs. No live fills. No xAI Grok Bot.

Sibling of `~/projects/worldmonitor` (optional context). This repo is the crew.

## What it does

CHIEF fans SCAN → VET → SIZE → FILLS (paper ledger), with RISK on Kraken OHLC. BOOK is skipped: prediction markets are not fillable here. A name without a Kraken `pair_id` never enters the list.

## Prerequisites

- Python 3
- Network to `api.kraken.com` public endpoints (no key)
- WorldMonitor optional (`npm run dev` in `~/projects/worldmonitor`)

## Universe

```bash
cd ~/projects/trading-desk
python3 scripts/kraken_universe.py
```

Writes `ledger/universe-kraken.json`: online USD/ZUSD spot pairs, no `.d` dark pools.

## Run the desk

Open this folder in a Cursor agent chat and type `/desk-cycle`.

That is the whole schedule. CHIEF fans the seats, writes the ledger, then arms a 15 minute `/loop` so the same chat wakes and runs `/desk-cycle` again. RISK uses hourly OHLC, so faster ticks would not see new bars.

Leave the chat open. Closing Cursor stops the desk.

There is no systemd timer and no Python stand-in for SCAN, VET, SIZE, FILLS, or RISK.

## Paper bank

Paper bank and halt pot are `bank.startingUsd` / `bank.allocatedUsd` (Kraken USD spot). If equivalent equity falls to 50% of allocated (`risk.haltDrawdown`), `ledger/halt.json` is written and new fills stop until you clear it.

Query-only live balance (uses `pass`, no orders):

```bash
python3 scripts/kraken_balance.py
```

`venues.scan` is `"kraken"`. `venues.fills` is `"paper"`. Seats never POST `AddOrder`.

## Seats

| Seat | Model | Owns |
|------|-------|------|
| CHIEF | cursor-grok-4.6-high | universe, fan-out, report |
| SCAN | composer-2.5 | Kraken ticker ranking |
| VET | cursor-grok-4.6-high | rejections |
| BOOK | unused | SKIPPED_UNIVERSE |
| SIZE | cursor-grok-4.6-high | dollars only |
| FILLS | composer-2.5 | paper ledger rows |
| RISK | cursor-grok-4.6-high | OHLC 6h/24h close rule |

## Dry-run tests

```bash
cd ~/projects/trading-desk
python3 tests/test_dry_run.py
```

Ticket math, halt floor, and a Kraken public universe fetch. No live orders. The seats are not in this test.

## Out of scope

- Live orders
- Pump.fun / FOMO / Polymarket / Kalshi
- Trading 212
- Printing `pass` secrets
- `/make-bot-ui` operator panel (later)
