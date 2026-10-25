#!/usr/bin/env bash
# Calibration runs: no model, just the environment and the verifier.
#
#   oracle  replays solution/solve.sh; its collect-hook snapshot is the task's
#           ground-truth world (gt_env.json) and its reward should be near 1.0
#   nop     does nothing; its reward is the null floor a real run has to beat
#
#   bash calibrate.sh <task-uuid> <oracle|nop> <job-name>
set -euo pipefail
H="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; export H
TASK=$1 AGENT=$2 JOB=$3
# the judge env exactly as a real run gets it: model and transport from .env / models.yaml,
# the codex login base64-encoded (a missing login only leaves the rubric channel unscored).
# A JUDGE_MODEL / JUDGE_TRANSPORT already in the environment (an orchestrator naming the judge
# its runs use) survives the .env sourcing, so calibration is judged by the same model as the runs.
PRESET_MODEL="${JUDGE_MODEL:-}" PRESET_TRANSPORT="${JUDGE_TRANSPORT:-}"
[ -f "$H/.env" ] && { set -a; . "$H/.env"; set +a; }
[ -n "$PRESET_MODEL" ] && export JUDGE_MODEL="$PRESET_MODEL"
[ -n "$PRESET_TRANSPORT" ] && export JUDGE_TRANSPORT="$PRESET_TRANSPORT"
eval "$("$H/.venv/bin/python" - <<'PYEOF'
import os, shlex, sys
sys.path.insert(0, os.environ["H"])
from harness.common import load_models
from harness.runjob import judge_env
d = load_models().get("defaults") or {}
model = os.environ.get("JUDGE_MODEL") or str(d.get("judge_model") or "")
transport = os.environ.get("JUDGE_TRANSPORT") or str(d.get("judge_transport") or "codex")
for k, v in judge_env(model, transport).items():
    print(f"export {k}={shlex.quote(v)}")
PYEOF
)"
cd "$H/out"
exec "$H/.venv/bin/harbor" run -p "$H/out/build/$TASK" -a "$AGENT" -k 1 -n 1 -y --job-name "$JOB"
