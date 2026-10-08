# omarchy-trading

Live Kraken USD spot desk (agent-driven SCAN → VET → SIZE → FILLS → RISK) plus the **evo.trading** Omarchy bar widget.

## Omarchy bar

- Bar: chart mark plus formatted **equity** (e.g. ` $606`). The mark stays up before the first quote.
- Panel: cash, pot, open positions, last cycle report, runner status
- Middle-click the bar icon to open the desk in the terminal

## Install plugin

```bash
omarchy plugin add /home/seb/projects/tradingbot
# or symlink via hyprdots install.sh → ~/.config/omarchy/plugins/evo.trading
omarchy plugin enable evo.trading
```

Add `evo.trading` to `shell.json` `plugins` and bar `layout` (see hyprdots).

## Hourly LLM loop (no Cursor chat tab)

```bash
systemctl --user link ~/projects/tradingbot/systemd/evo-trading-desk.{service,timer}
systemctl --user enable --now evo-trading-desk.timer
```

Each tick runs `bin/run-desk-cycle` → `cursor-agent -p "/desk-cycle"` (CHIEF + seats unchanged).

Logs: `~/.local/state/evoshell/trading/desk-cycle.log`

## Exit check

While a position is open, a 5-minute timer sells on the trail, or on dead volume while the trail is not armed. A flat book does not call Kraken.

```bash
systemctl --user link ~/projects/tradingbot/systemd/evo-trading-exit.{service,timer}
systemctl --user enable --now evo-trading-exit.timer
```

`bin/run-desk-exit` → `scripts/desk_exit_check.py` → `kraken_execute.py sell`.

## Manual cycle

Open this repo in Cursor and run `/desk-cycle`, or:

```bash
~/projects/tradingbot/bin/run-desk-cycle
```

## Scripts

```bash
python3 scripts/kraken_universe.py
python3 scripts/desk_context.py
python3 scripts/kraken_balance.py
python3 scripts/desk_status.py   # JSON for the bar
```

## Config

`config.json` — paper, Kelly, risk trail, Kraken `pass` key names (`evoshell/kraken/trading-desk-key`).

## Tests

```bash
cd ~/projects/tradingbot
python3 tests/test_dry_run.py
```
