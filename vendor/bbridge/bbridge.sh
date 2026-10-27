#!/usr/bin/env bash
# bbridge launcher: Amazon Bedrock Converse (GLM 5.3 via an application inference
# profile) behind an Anthropic-shaped /v1/messages.
#
#   ./bbridge.sh start | stop | status | check
#
# Reads BB_BEDROCK_API_KEY, BB_MODEL_ARN, BB_REGION, BB_BRIDGE_SECRET and
# BB_THINKING_SIG_KEY from .env (mode 600). Binds 0.0.0.0 so task containers reach
# it as host.docker.internal:8767; BB_HOST=127.0.0.1 keeps it on loopback (Mac only).
# Callers authenticate with the bridge secret as x-api-key (LLM_API_KEY for the agent).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${BB_HOST:-0.0.0.0}"
PROBE_HOST=127.0.0.1
PORT="${BB_PORT:-8767}"
PID_FILE="$HERE/.bbridge.pid"
LOG_FILE="$HERE/logs/bbridge.log"

[ -s "$HERE/.env" ] || { echo "[bbridge] no $HERE/.env" >&2; exit 1; }
set -a; . "$HERE/.env"; set +a
export BB_STREAM_LOG_PATH="${BB_STREAM_LOG_PATH:-$HERE/logs/stream_tee.jsonl}"

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-start}" in
  start)
    if running; then echo "[bbridge] already running (PID $(cat "$PID_FILE")) on http://$HOST:$PORT"; exit 0; fi
    mkdir -p "$HERE/logs"
    cd "$HERE"
    nohup .venv/bin/python -m bbridge --host "$HOST" --port "$PORT" --log-level info >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    for _ in $(seq 1 20); do
      curl -fsS --max-time 3 "http://$PROBE_HOST:$PORT/healthz" >/dev/null 2>&1 && { echo "[bbridge] up on http://$HOST:$PORT (PID $(cat "$PID_FILE"))"; exit 0; }
      sleep 0.5
    done
    echo "[bbridge] FAILED to come up; tail of $LOG_FILE:" >&2; tail -20 "$LOG_FILE" >&2; exit 1 ;;
  stop)
    if running; then kill "$(cat "$PID_FILE")"; echo "[bbridge] stopped PID $(cat "$PID_FILE")"; else echo "[bbridge] not running"; fi
    rm -f "$PID_FILE" ;;
  status)
    if running; then echo "[bbridge] running (PID $(cat "$PID_FILE")) on http://$HOST:$PORT"; curl -fsS --max-time 5 "http://$PROBE_HOST:$PORT/healthz"; echo
    else echo "[bbridge] not running"; exit 1; fi ;;
  check)
    cd "$HERE"; exec .venv/bin/python -m bbridge --check ;;
  *) echo "usage: $0 start|stop|status|check" >&2; exit 2 ;;
esac
