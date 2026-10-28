# omarchy-trading

Live Kraken USD spot desk plus the **evo.trading** Omarchy bar widget. Once an hour Grok decides and can place orders. A one-minute runner acts only on rules it set. The 5-minute exit timer still guards positions that have no rule yet.

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

## Hourly wake

Grok 4.7 decides once an hour (`desk-wake.timer`, at :02). The bar badge follows that wake, not the old entry cycle. Orders go to Kraken only while `desk.live` is true. Each order needs a `--reason`, stored in `state/grok.db`.

```bash
systemctl --user link ~/projects/tradingbot/systemd/desk-wake.{service,timer}
systemctl --user enable --now desk-wake.timer
systemctl --user link ~/projects/tradingbot/systemd/desk-runner.{service,timer}
systemctl --user enable --now desk-runner.timer
```

`bin/desk wake` builds the snapshot and resumes the chat. `bin/desk run` is the one-minute pass that sells or buys only when a rule Grok set says so. `bin/desk trades` prints the log.

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
python3 -m unittest tests.test_grok_desk
```
