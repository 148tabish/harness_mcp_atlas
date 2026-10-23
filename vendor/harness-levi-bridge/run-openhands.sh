#!/usr/bin/env bash
# Run a fixed-tasks task with harbor's OpenHands SDK agent on the Claude Code
# subscription, through the bridge in this directory.
#
#   ./run-openhands.sh -p ../fixed-tasks/<task-id> [-k 8] [--job-name NAME] [...]
#
# Every argument goes to `harbor run` unchanged, after these defaults:
#   -a openhands_bridge_agent:OpenHandsSDKBridge   (harbor_agent/, see its docstring)
#   -m anthropic/claude-opus-5                     (override with MODEL=...)
# and ../fixed-tasks/run-harbor.sh picks ECR or local images as usual.
#
# Environment it sets up:
#   LLM_BASE_URL    the bridge as the task containers reach it (BRIDGE_URL, default http://host.docker.internal:8765)
#   LLM_API_KEY     the stub key the bridge strips
#   CODEX_AUTH_B64  ~/.codex/auth.json, so the verifier's codex judge scores the rubric
#   HARBOR          .venv-harbor/bin/harbor (harbor 0.24.0)
#
# The bridge must already be up: ./claude_code_bridge.sh start
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASKS="${FIXED_TASKS_DIR:-$HERE/../fixed-tasks}"
PORT="${AURORA_CC_BRIDGE_PORT:-8765}"

if ! curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "[run-openhands] the bridge is not answering on :${PORT}; start it with:" >&2
    echo "    AURORA_CC_PYTHON=$HERE/.venv/bin/python $HERE/claude_code_bridge.sh start" >&2
    exit 1
fi

export LLM_BASE_URL="${BRIDGE_URL:-http://host.docker.internal:${PORT}}"
export LLM_API_KEY="${LLM_API_KEY:-sk-ant-oauth-bridge-stub}"
export PYTHONPATH="$HERE/harbor_agent${PYTHONPATH:+:$PYTHONPATH}"
export HARBOR="${HARBOR:-$HERE/.venv-harbor/bin/harbor}"
if [ -z "${CODEX_AUTH_B64:-}" ] && [ -s "$HOME/.codex/auth.json" ]; then
    CODEX_AUTH_B64="$(base64 < "$HOME/.codex/auth.json" | tr -d '\n')"   # GNU and BSD base64
    export CODEX_AUTH_B64
fi
[ -n "${CODEX_AUTH_B64:-}" ] || echo "[run-openhands] no ~/.codex/auth.json: the rubric will be unscored" >&2

echo "[run-openhands] model ${MODEL:-anthropic/claude-opus-5} via ${LLM_BASE_URL}" >&2
exec "$TASKS/run-harbor.sh" run \
    -a openhands_bridge_agent:OpenHandsSDKBridge \
    -m "${MODEL:-anthropic/claude-opus-5}" \
    "$@"
