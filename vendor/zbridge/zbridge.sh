#!/usr/bin/env bash
# zbridge launcher: z.ai GLM Coding Plan behind an Anthropic-shaped /v1/messages.
#
#   ./zbridge.sh start | stop | status | check
#
# Reads ZB_ZAI_API_KEY, ZB_BRIDGE_SECRET and ZB_THINKING_SIG_KEY from .env (mode 600).
# Binds 0.0.0.0 so task containers reach it as host.docker.internal:8766 on a Linux engine
# (docker0 gateway) and on Docker Desktop alike; ZB_HOST=127.0.0.1 keeps it on loopback (Mac only)
# while it stays off the public interface. Callers authenticate with the bridge
# secret as x-api-key (LLM_API_KEY for the OpenHands agent).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${ZB_HOST:-0.0.0.0}"
PROBE_HOST=127.0.0.1
PORT="${ZB_PORT:-8766}"
PID_FILE="$HERE/.zbridge.pid"
LOG_FILE="$HERE/logs/zbridge.log"

[ -s "$HERE/.env" ] || { echo "[zbridge] no $HERE/.env" >&2; exit 1; }
set -a; . "$HERE/.env"; set +a
export ZB_STREAM_LOG_PATH="${ZB_STREAM_LOG_PATH:-$HERE/logs/stream_tee.jsonl}"

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-start}" in
  start)
    if running; then echo "[zbridge] already running (PID $(cat "$PID_FILE")) on http://$HOST:$PORT"; exit 0; fi
    mkdir -p "$HERE/logs"
    cd "$HERE"
    nohup .venv/bin/python -m zbridge --host "$HOST" --port "$PORT" --log-level info >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    for _ in $(seq 1 20); do
      curl -fsS --max-time 3 "http://$PROBE_HOST:$PORT/healthz" >/dev/null 2>&1 && { echo "[zbridge] up on http://$HOST:$PORT (PID $(cat "$PID_FILE"))"; exit 0; }
      sleep 0.5
    done
    echo "[zbridge] FAILED to come up; tail of $LOG_FILE:" >&2; tail -20 "$LOG_FILE" >&2; exit 1 ;;
  stop)
    if running; then kill "$(cat "$PID_FILE")"; echo "[zbridge] stopped PID $(cat "$PID_FILE")"; else echo "[zbridge] not running"; fi
    rm -f "$PID_FILE" ;;
  status)
    if running; then echo "[zbridge] running (PID $(cat "$PID_FILE")) on http://$HOST:$PORT"; curl -fsS --max-time 5 "http://$PROBE_HOST:$PORT/healthz"; echo
    else echo "[zbridge] not running"; exit 1; fi ;;
  check)
    cd "$HERE"; exec .venv/bin/python -m zbridge --check ;;
  *) echo "usage: $0 start|stop|status|check" >&2; exit 2 ;;
esac
