#!/usr/bin/env bash
# One task in, one packaged deliverable out.
#
#   ./run.sh --task input/wraysbury --model glm-5.3-flash -k 8 -n 2
#   ./run.sh --task input/wraysbury --model opus-5 --effort low -k 8 -n 2
#   ./run.sh --task input/wraysbury --only build          # just materialise it
#   ./run.sh --task input/wraysbury --only package --job out/jobs/<name>
#
# Steps: validate the input folder, build it into a harbor task under out/build/,
# start the bridge the model needs, run harbor, report per-trial scores, then
# package the job into out/packaged/<uuid>/. Every argument is passed through to
# harness.cli; see --help there for the full list.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -x "$HERE/.venv/bin/python" ] || { echo "no venv; run $HERE/setup.sh first" >&2; exit 1; }
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

cd "$HERE"
exec .venv/bin/python -m harness.cli "$@"
