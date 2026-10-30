"""8% stop under entry, or a 5% giveback once the high is 8% above entry.

Reads the desk context on stdin and prints hold or sell. A bad context holds.
"""
import json
import sys

STOP = 0.08
ARM = 0.08
GIVEBACK = 0.05


def main() -> None:
    ctx = json.load(sys.stdin)
    last, entry, peak = ctx.get("last"), ctx.get("entry"), ctx.get("peak")
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
    if peak is not None and (float(peak) - entry) / entry >= ARM:
        if last <= float(peak) * (1.0 - GIVEBACK):
            print("sell")
            return
    print("hold")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("hold")
