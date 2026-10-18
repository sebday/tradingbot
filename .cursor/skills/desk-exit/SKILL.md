---
name: desk-exit
description: Run one extra Kraken desk exit check by hand (stop-loss, trailing stop, dead volume) and report holds and closes. Use for manual /desk-exit only.
disable-model-invocation: true
---

# Desk exit (manual)

The scheduled exit is `evo-trading-exit.timer` running `scripts/desk_exit_check.py` every 5 minutes. Run this only when a human asks for an extra look.

```bash
cd ~/projects/tradingbot && TRADING_DESK_LEDGER=$PWD/ledger python3 scripts/desk_exit_check.py
```

Rules, in order, from `scripts/desk_rules.py`:

1. `STOP_LOSS`: last at or under entry × (1 − `risk.stopLossPct`).
2. `TRAIL_PEAK`: once the peak since the episode opened is `risk.trailArmPct` over entry, close at the higher of peak × (1 − `trailGiveback`) and entry × (1 + `trailFloorPct`).
3. `VOLUME_6H`: trail not armed and six hour volume under `risk.volumeRatioClose` of the 24 hour average.

A position with no price is `STALE`: held, never sold, and alerted after `risk.staleAlertMinutes`.

Report each position's action, rule, % vs entry, and any sell result. Never call `kraken_execute.py sell` yourself and never edit `ledger/halt.json`.
