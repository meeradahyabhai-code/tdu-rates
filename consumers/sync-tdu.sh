#!/usr/bin/env bash
# Pull the canonical TDU rates into a project that isn't on GitHub (bill-check).
#   ./sync-tdu.sh ~/bill-check/data/tdsp_charges.csv
# Refuses to overwrite with anything that doesn't look like the real file.
set -euo pipefail

CANONICAL=${CANONICAL:-https://raw.githubusercontent.com/meeradahyabhai-code/tdu-rates/main/data/tdsp_charges.csv}
target=${1:?usage: sync-tdu.sh <path to tdsp_charges.csv>}
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

curl --fail-with-body -sSL "$CANONICAL" -o "$tmp"

head -1 "$tmp" | grep -qx 'utility,monthly,perKwh,startDate,endDate' \
  || { echo "unexpected header, not writing:"; head -3 "$tmp"; exit 1; }
lines=$(wc -l < "$tmp")
[ "$lines" -ge 100 ] || { echo "only $lines lines, not writing"; exit 1; }
if [ -f "$target" ]; then
  [ "$lines" -ge "$(wc -l < "$target")" ] || { echo "canonical is shorter than $target, not writing"; exit 1; }
  if cmp -s "$tmp" "$target"; then echo "$target already current"; exit 0; fi
  diff "$target" "$tmp" | grep -E '^[<>] [A-Z]' || true
fi
cp "$tmp" "$target"
echo "updated $target"
