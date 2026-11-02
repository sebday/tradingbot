"""8% stop under entry, or a sell once the one-month punt is over.

Reads the desk context on stdin and prints hold or sell. A bad context holds.
The punt ends at 2026-12-02T18:00:00Z.
"""
import json
import sys
from datetime import datetime, timezone

STOP = 0.08
DEADLINE = datetime(2026, 12, 2, 18, 0, tzinfo=timezone.utc).timestamp()


def main() -> None:
    ctx = json.load(sys.stdin)
    last, entry, now = ctx.get("last"), ctx.get("entry"), ctx.get("now")
    if last is None or not entry:
        print("hold")
        return
    last = float(last)
    entry = float(entry)
    if entry <= 0:
        print("hold")
        return
    if last <= entry * (1.0 - STOP):
        print("sell")
        return
    if now is not None and float(now) >= DEADLINE:
        print("sell")
        return
    print("hold")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("hold")
