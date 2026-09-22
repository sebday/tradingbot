# omarchy-trading

Live Kraken USD spot desk (agent-driven SCAN → VET → SIZE → FILLS → RISK) plus the **evo.trading** Omarchy bar widget.

## Omarchy bar

- Bar: formatted **equity** (e.g. `$606`)
- Panel: cash, pot, open positions, last cycle report, runner status
- Actions: Refresh, **Run cycle** (full LLM `/desk-cycle`), Open desk (default agent in the centered terminal popup)

## Install plugin

```bash
omarchy plugin add /home/seb/projects/omarchy-trading
# or symlink via hyprdots install.sh → ~/.config/omarchy/plugins/evo.trading
omarchy plugin enable evo.trading
```

Add `evo.trading` to `shell.json` `plugins` and bar `layout` (see hyprdots).

## Hourly LLM loop (no Cursor chat tab)

```bash
systemctl --user link ~/projects/omarchy-trading/systemd/omarchy-trading-desk.{service,timer}
systemctl --user enable --now omarchy-trading-desk.timer
```

Each tick runs `bin/run-desk-cycle` → `cursor-agent -p "/desk-cycle"` (CHIEF + seats unchanged).

Logs: `~/.local/state/omarchy/trading/desk-cycle.log`

## Manual cycle

Open this repo in Cursor and run `/desk-cycle`, or:

```bash
~/projects/omarchy-trading/bin/run-desk-cycle
```

## Scripts

```bash
python3 scripts/kraken_universe.py
python3 scripts/desk_context.py
python3 scripts/kraken_balance.py
python3 scripts/desk_status.py   # JSON for the bar
```

## Config

`config.json` — paper, Kelly, risk trail, Kraken `pass` key names (`kraken/trading-desk-key`).

## Tests

```bash
cd ~/projects/omarchy-trading
python3 tests/test_dry_run.py
```
