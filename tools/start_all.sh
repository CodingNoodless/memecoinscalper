#!/usr/bin/env bash
# Start every long-running paper bot / recorder that is not already running (vault: "Data & Infra/Running programs").
# Paper only; nothing here signs a transaction. Safe to re-run: a program whose command line is already live is skipped.
#   bash tools/start_all.sh          # start what's missing
#   bash tools/start_all.sh --list   # only show what is running / missing
cd /c/Users/Rohit/PycharmProjects/tradeslavebot || exit 1
mkdir -p logs

# pattern (regex on the python command line) | command (after `uv run python`) | log file
PROGRAMS=(
  'research\.live_feed\b|-u -m tradeslave.research.live_feed|live_feed'
  'research\.launch_recorder\b|-u -m tradeslave.research.launch_recorder|launch_recorder'
  'research\.chain_backfill\b|-u -m tradeslave.research.chain_backfill|chain_backfill'
  'research\.launch_watch\b|-m tradeslave.research.launch_watch|launch_watch'
  'research\.c1_curve record|-m tradeslave.research.c1_curve record|c1_curve'
  'research\.create_poll\b|-X utf8 -u -m tradeslave.research.create_poll|create_poll'
  'm1_bot run\s*$|-u -m tradeslave.research.m1_bot run|m1_bot'
  'm1_bot run m2\s*$|-u -m tradeslave.research.m1_bot run m2|m2_bot'
  'm1_bot run m3\s*$|-u -m tradeslave.research.m1_bot run m3|m3_bot'
  'm1_bot run m4\s*$|-u -m tradeslave.research.m1_bot run m4|m4_bot'
  'm1_bot run m5\s*$|-u -m tradeslave.research.m1_bot run m5|m5_bot'
  'm1_bot run m6\s*$|-u -m tradeslave.research.m1_bot run m6|m6_bot'
  'm1_bot run m7\s*$|-X utf8 -u -m tradeslave.research.m1_bot run m7|m7_bot'
  'm1_bot run m9\s*$|-X utf8 -u -m tradeslave.research.m1_bot run m9|m9_bot'
  'research\.m11_bot\b|-u -m tradeslave.research.m11_bot|m11_bot'
  'research\.m13_bot\b|-X utf8 -u -m tradeslave.research.m13_bot|m13_bot'
  'research\.hy_record record|-u -m tradeslave.research.hy_record record|hy_record'
  'research\.dl_record record|-u -m tradeslave.research.dl_record record|dl_record'
  'research\.liq_map scan|-u -m tradeslave.research.liq_map scan|liq_map'
  'research\.perp_forward --loop|-u -m tradeslave.research.perp_forward --loop|perp_forward'
  'kalshi_lip paper\s*$|-u -m tradeslave.research.kalshi_lip paper|kalshi_lip_paper'
  'kalshi_lip paper front\s*$|-u -m tradeslave.research.kalshi_lip paper front|kalshi_lip_front'
  'kalshi_lip paper mm\s*$|-u -m tradeslave.research.kalshi_lip paper mm|kalshi_lip_mm'
  'kalshi_lip paper mm join\s*$|-u -m tradeslave.research.kalshi_lip paper mm join|kalshi_lip_mmjoin'
  'dashboard\.py|dashboard.py|dashboard'
)

running=$(powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | ForEach-Object { \$_.CommandLine }" | tr -d '\r')
for row in "${PROGRAMS[@]}"; do
  IFS='|' read -r pat cmd log <<< "$row"
  if echo "$running" | grep -Eq "$pat"; then
    echo "running  $log"
  elif [ "$1" = "--list" ]; then
    echo "MISSING  $log"
  else
    echo "starting $log  (logs/$log.out)"
    PYTHONIOENCODING=utf-8 nohup uv run python $cmd >> "logs/$log.out" 2>&1 &
    sleep 2
  fi
done
