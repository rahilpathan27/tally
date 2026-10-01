#!/usr/bin/env bash
# 100,000 full-flow chaos scenarios of one seed, as four parallel shards on separate databases.
# Needs the Compose stack (make up). Reports land in .data/chaos-100k/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/.data/chaos-100k"
SEED="${SEED:-20261001}"
TOTAL="${TOTAL:-100000}"
SHARDS="${SHARDS:-4}"
PER=$((TOTAL / SHARDS))
mkdir -p "$OUT"
cd "$ROOT"
pids=()
for i in $(seq 0 $((SHARDS - 1))); do
  uv run python -m chaos.flow_sim --seed "$SEED" --start $((i * PER)) --scenarios "$PER" \
    --check-every 1000 --db-prefix "tally_chaos_s$i" >"$OUT/shard-$i.log" 2>&1 &
  pids+=($!)
done
status=0
for pid in "${pids[@]}"; do wait "$pid" || status=1; done
grep -h "TALLY FLOW CHAOS" "$OUT"/shard-*.log || true
exit $status
