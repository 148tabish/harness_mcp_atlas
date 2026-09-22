#!/usr/bin/env bash
# Calibration runs: no model, just the environment and the verifier.
#
#   oracle  replays solution/solve.sh; its collect-hook snapshot is the task's
#           ground-truth world (gt_env.json) and its reward should be near 1.0
#   nop     does nothing; its reward is the null floor a real run has to beat
#
#   bash calibrate.sh <task-uuid> <oracle|nop> <job-name>
set -euo pipefail
H="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASK=$1 AGENT=$2 JOB=$3
export CODEX_AUTH_B64="$(base64 -w0 ~/.codex/auth.json)" JUDGE_MODEL=gpt-5.6-sol
cd "$H/out"
exec "$H/.venv/bin/harbor" run -p "$H/out/build/$TASK" -a "$AGENT" -k 1 -n 1 -y --job-name "$JOB"
