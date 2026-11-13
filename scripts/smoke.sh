#!/usr/bin/env bash
# 容器启动后的最小冒烟测试（仅依赖 curl）。
# 用法：bash scripts/smoke.sh [base_url，默认 http://localhost:8001]
set -euo pipefail

BASE="${1:-http://localhost:8001}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"

say() { printf '\n--- %s\n' "$*"; }

say "healthz"
curl -fsS "$BASE/healthz"; echo

say "baseline batch (expect 200)"
curl -fsS -F "file=@$ROOT/samples/01_baseline.csv" "$BASE/api/readings/batch"; echo

say "legal correction (expect 200, intervals rebuilt)"
curl -fsS --data-binary "@$ROOT/samples/04_correction_legal.csv" \
     -H 'Content-Type: text/csv' "$BASE/api/readings/batch"; echo

say "regression batch (expect HTTP 409 with conflicts)"
curl -sS -o /tmp/regression.json -w 'HTTP %{http_code}\n' \
     --data-binary "@$ROOT/samples/05_regression_rejected.csv" \
     -H 'Content-Type: text/csv' "$BASE/api/readings/batch"
cat /tmp/regression.json; echo

say "in-batch regression (expect HTTP 409)"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
     --data-binary "@$ROOT/samples/06_batch_regression.csv" \
     -H 'Content-Type: text/csv' "$BASE/api/readings/batch"

say "row-limit / bad-csv checks (expect 400)"
curl -sS -o /dev/null -w 'bad header: HTTP %{http_code}\n' \
     --data-binary 'a,b,c' -H 'Content-Type: text/csv' "$BASE/api/readings/batch"

say "query readings + intervals (single snapshot)"
curl -fsS "$BASE/api/meters/MW-001"; echo
curl -fsS "$BASE/api/meters"; echo

say "smoke test finished"
