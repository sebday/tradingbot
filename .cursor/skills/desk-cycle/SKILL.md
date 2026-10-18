---
name: desk-cycle
description: Run one hourly Kraken desk cycle by hand (context, mechanical shortlist, /desk-vet veto, execute, exit pass) and summarise the report. Use for /desk-cycle, run the desk, or run a trading cycle.
disable-model-invocation: true
---

# Desk cycle

The cycle is a script. Do not reproduce it with subagents and do not make trading decisions here.

```bash
~/projects/tradingbot/bin/run-desk-cycle
```

That runs, in order:

1. `scripts/desk_context.py`: fear and greed, VIX, CoinPaprika into `ledger/context.json`.
2. `scripts/desk_cycle.py shortlist`: universe, ticker gates (volume, spread, day band, 24h range), stablecoin and fiat exclusions, and book guards (already held, re-entry cooldown, max open, max new per day) into `ledger/shortlist.json`.
3. `cursor-agent -p "/desk-vet"`: the only LLM step. It can veto shortlisted names and nothing else. No verdicts means no buys.
4. `scripts/desk_cycle.py execute`: balance, reconcile, sizing (6% of equity, $10 floor, 50% exposure cap), then one limit IOC buy, or a shadow row while `entry.enabled` is false.
5. `scripts/desk_exit_check.py`: the same exit pass the 5-minute timer runs.

After it finishes, read the last `kind: "cycle"` row in `ledger/reports.jsonl` and the tail of `~/.local/state/evoshell/trading/desk-cycle.log`, and summarise: equity, cash, open positions, shortlist, vetoes, each decision's reason, fills or shadow fills, and any exits.

## Hard limits

- Never call `scripts/kraken_execute.py` yourself.
- Never edit `ledger/halt.json`. If it is `HALTED`, tell the human.
- Never edit `config.json` unless the human asks.

## Timers

```bash
systemctl --user link ~/projects/tradingbot/systemd/evo-trading-desk.{service,timer}
systemctl --user enable --now evo-trading-desk.timer
systemctl --user link ~/projects/tradingbot/systemd/evo-trading-exit.{service,timer}
systemctl --user enable --now evo-trading-exit.timer
```

Do not arm a bash sleep loop or `AGENT_LOOP_TICK_desk-cycle`.
