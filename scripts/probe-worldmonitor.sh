#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${WM_PORT:-}"
if [[ -z "$PORT" && -f "$ROOT/config.json" ]]; then
  PORT="$(python3 -c 'import json; print(json.load(open("'"$ROOT"'/config.json"))["wmPort"])')"
fi
PORT="${PORT:-3000}"
BASE="http://localhost:${PORT}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch() {
  local path="$1"
  local out="$2"
  local url="${BASE}${path}"
  echo "== GET ${url}" >&2
  curl -fsS --max-time 20 "$url" -o "$out"
}

retries=10
sleep_s=3
for i in $(seq 1 "$retries"); do
  echo "-- attempt $i/$retries"
  set +e
  fetch "/api/market/v1/get-fear-greed-index" "$TMP/fg.json"
  fg=$?
  fetch "/api/prediction/v1/list-prediction-markets?page_size=1" "$TMP/pred.json"
  pred=$?
  fetch "/api/intelligence/v1/list-cross-source-signals" "$TMP/sig.json"
  sig=$?
  set -e
  if [[ "$fg" -ne 0 || "$pred" -ne 0 || "$sig" -ne 0 ]]; then
    echo "curl failed fg=$fg pred=$pred sig=$sig"
    sleep "$sleep_s"
    continue
  fi
  python3 - "$TMP/fg.json" "$TMP/pred.json" "$TMP/sig.json" "$PORT" <<'PY'
import json, sys
def load(path):
    return json.loads(open(path).read())
fg_d = load(sys.argv[1])
pred_d = load(sys.argv[2])
sig_d = load(sys.argv[3])
port = sys.argv[4]
degraded = pred_d.get("dataAvailable") is not True or pred_d.get("fetchedAt") in (None, 0, "0")
print("ok fear-greed", "unavailable" if fg_d.get("unavailable") else "present")
print("ok signals keys=", ",".join(list(sig_d)[:8]))
if degraded:
    print("PREDICTION_DEGRADED dataAvailable=", pred_d.get("dataAvailable"), "fetchedAt=", pred_d.get("fetchedAt"))
    print("BOOK must stop. Local WorldMonitor prediction list needs Redis bootstrap; never fall back to api.worldmonitor.app.")
    print(f"PARTIAL_READY wmPort={port}")
    sys.exit(2)
print("ok prediction dataAvailable true")
print(f"READY wmPort={port}")
PY
  status=$?
  if [[ "$status" -eq 0 || "$status" -eq 2 ]]; then
    exit "$status"
  fi
  sleep "$sleep_s"
done

echo "FAILED: WorldMonitor did not return three parseable 200s on ${BASE}" >&2
echo "Never fall back to api.worldmonitor.app." >&2
exit 1
