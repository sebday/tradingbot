#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Local WorldMonitor UI is unused. Context lives in this repo.
exec python3 "$ROOT/scripts/desk_context.py"
