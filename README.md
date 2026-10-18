# omarchy-trading

Live Kraken USD spot desk plus the **evo.trading** Omarchy bar widget. Entries and exits are deterministic Python; the only LLM step is a veto over the hourly shortlist.

## Omarchy bar

- Bar: chart mark plus formatted **equity** (e.g. ` $606`). The mark stays up before the first quote.
- Panel: cash, pot, open positions, last cycle report, runner status
- Middle-click the bar icon to open the desk in the terminal

## Install plugin

```bash
omarchy plugin add /home/seb/projects/tradingbot
# or symlink via hyprdots install.sh → ~/.config/omarchy/plugins/evo.trading
omarchy plugin enable evo.trading
```

Add `evo.trading` to `shell.json` `plugins` and bar `layout` (see hyprdots).

## Hourly cycle

```bash
systemctl --user link ~/projects/tradingbot/systemd/evo-trading-desk.{service,timer}
systemctl --user enable --now evo-trading-desk.timer
```

`bin/run-desk-cycle` runs:

1. `scripts/desk_context.py`: market context into `ledger/context.json`
2. `scripts/desk_cycle.py shortlist`: gates, stablecoin and fiat exclusions, and book guards into `ledger/shortlist.json`
3. `cursor-agent -p "/desk-vet"`: the LLM may veto shortlisted names, nothing else. No verdicts means no buys.
4. `scripts/desk_cycle.py execute`: balance, reconcile, sizing, one limit IOC buy (or a shadow row)
5. `scripts/desk_exit_check.py`: an exit pass

Logs: `~/.local/state/evoshell/trading/desk-cycle.log`

### Entry guards

- Never adds to a pair already held, and waits `entry.reentryCooldownHours` after a close
- At most `entry.maxNewPerDay` new positions per rolling 24 hours, and `maxOpenBooks` open
- Ticket is `entry.ticketPctEquity` of equity, at least `entry.minTicketUsd`, and total exposure stays under `entry.maxExposurePct` of equity
- Buys are limit IOC at ask + `entry.maxSlippageBps`
- `entry.enabled: false` is shadow mode: decisions go to `ledger/shadow.jsonl`, nothing is bought

## Exit check

A 5-minute timer checks every open position (`scripts/desk_rules.py`):

1. `STOP_LOSS`: `risk.stopLossPct` under entry
2. `TRAIL_PEAK`: armed at `risk.trailArmPct` over entry, closes on the giveback floor
3. `VOLUME_6H`: dead volume while the trail is not armed

A position with no price is held and alerted (`notify-send`) after `risk.staleAlertMinutes`, never sold blind. A flat book does not call Kraken.

```bash
systemctl --user link ~/projects/tradingbot/systemd/evo-trading-exit.{service,timer}
systemctl --user enable --now evo-trading-exit.timer
```

## Positions

`scripts/desk_book.py` rebuilds positions from `ledger/fills.jsonl` and `ledger/closes.jsonl`. A pair bought again after a full close is a new episode with its own entry and peak. Each hourly execute reconciles the book against Kraken balances into `ledger/reconcile.json`, and the exit check also manages untracked holdings found there.

## Scripts

```bash
python3 scripts/desk_book.py       # open positions and realised P&L
python3 scripts/kraken_universe.py
python3 scripts/desk_context.py
python3 scripts/kraken_balance.py
python3 scripts/desk_status.py     # JSON for the bar
```

## Config

`config.json`: `paper`, `entry`, `risk`, `scan`, and the Kraken `pass` key names (`evoshell/kraken/trading-desk-key`).

## Tests

```bash
cd ~/projects/tradingbot
python3 tests/test_dry_run.py
```
