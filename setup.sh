#!/usr/bin/env bash
# One-time setup on a new machine: the top-level .env is the only file anyone
# edits. setup.sh creates it from .env.example, reads it, and pushes what each
# component needs into its own config (today: vendor/zbridge/.env). Safe to
# re-run; it keeps generated secrets and only fills what is missing.
#
#   ./setup.sh            # set up and report
#   ./setup.sh --check    # report only, change nothing
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1

# .env first: everything below reads from it
if [ ! -f "$HERE/.env" ] && [ "$CHECK_ONLY" = 0 ]; then
    umask 077; cp "$HERE/.env.example" "$HERE/.env"
    echo "wrote $HERE/.env from .env.example - put your keys in it"
fi
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }
fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mMISS\033[0m %s\n' "$1"; fail=1; }

echo "== prerequisites"
command -v docker >/dev/null 2>&1 && ok "docker $(docker --version | awk '{print $3}' | tr -d ,)" || bad "docker not installed"
docker info >/dev/null 2>&1 && ok "docker daemon reachable" || bad "cannot talk to the docker daemon (add your user to the docker group)"
docker compose version >/dev/null 2>&1 && ok "docker compose v2" || bad "docker compose v2 plugin missing"
command -v uv >/dev/null 2>&1 && ok "uv $(uv --version | awk '{print $2}')" || bad "uv not installed (curl -LsSf https://astral.sh/uv/install.sh | sh)"
# the venvs are built by uv, which fetches its own 3.12 if the system python is older
if python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null; then
    ok "python $(python3 -V | awk '{print $2}')"
elif command -v uv >/dev/null 2>&1 && uv python find 3.12 >/dev/null 2>&1; then
    ok "python 3.12 via uv (system python $(python3 -V 2>&1 | awk '{print $2}') is older)"
else
    bad "python 3.12 not available (install it, or let uv fetch one: uv python install 3.12)"
fi
if df -BG --output=avail "$HERE" >/dev/null 2>&1; then
    avail=$(df -BG --output=avail "$HERE" | tail -1 | tr -dc '0-9')     # GNU coreutils
else
    avail=$(df -g "$HERE" | awk 'NR==2 {print $4}')                      # BSD/macOS, 1G blocks
fi
[ "${avail:-0}" -ge 40 ] && ok "disk ${avail}G free" || warn "only ${avail}G free; 40G+ recommended"
for port in 8765 8766; do
    if { command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$port "; } \
       || { command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; }; then
        warn "port $port already in use (a bridge may already be running)"
    else
        ok "port $port free"
    fi
done

echo "== host"
OS=$(uname -s); ARCH=$(uname -m)
case "$ARCH" in x86_64|amd64) PLATFORM=linux/amd64 ;; arm64|aarch64) PLATFORM=linux/arm64 ;; *) PLATFORM="linux/$ARCH" ;; esac
ok "$OS $ARCH; images are pulled for $PLATFORM"
# The task containers reach the bridges as host.docker.internal; a Linux engine gets
# that name from the compose template's extra_hosts host-gateway (engine 20.10+).
ENGINE=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 0)
if python3 -c 'import sys; v=tuple(int(x) for x in sys.argv[1].split("-")[0].split(".")[:2]); raise SystemExit(0 if v >= (20,10) else 1)' "$ENGINE" 2>/dev/null; then
    ok "docker engine $ENGINE ($(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null), host-gateway supported)"
else
    bad "docker engine $ENGINE is older than 20.10: no host-gateway alias, task containers cannot reach the bridges"
fi
if [ "$CHECK_ONLY" = 1 ] || [ -n "${HARNESS_PROBE_GATEWAY:-}" ]; then
    # the real thing: a container resolving the alias (pulls a 3 MB image once)
    if docker run --rm --add-host host.docker.internal:host-gateway alpine:3.20 sh -c 'getent hosts host.docker.internal' >/dev/null 2>&1; then
        ok "a container resolves host.docker.internal"
    else
        warn "a container could not resolve host.docker.internal (offline, or an engine without host-gateway)"
    fi
fi
# resources as the engine sees them: the Docker Desktop VM on a Mac, the machine on Linux
NCPU=$(docker info --format '{{.NCPU}}' 2>/dev/null || echo 0)
MEMGB=$(( $(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0) / 1073741824 ))
CONC=$(sed -n 's/^ *concurrent: *\([0-9]*\).*/\1/p' "$HERE/config/models.yaml" | head -1); CONC=${CONC:-2}
# a trial with ~30 light servers takes ~2 GB and a core; the separate verifier and the
# judge another ~2 GB; the bridges and the OS the rest
NEED_GB=$(( CONC * 3 + 4 )); NEED_CPU=$(( CONC * 2 ))
if [ "$NCPU" -ge "$NEED_CPU" ] && [ "$MEMGB" -ge "$NEED_GB" ]; then
    ok "docker has $NCPU CPUs, ${MEMGB} GB: enough for $CONC concurrent trials"
else
    warn "docker has $NCPU CPUs, ${MEMGB} GB; $CONC concurrent trials want ~$NEED_CPU CPUs, ~${NEED_GB} GB (lower -n / defaults.concurrent, or raise Docker Desktop's resources)"
fi
command -v codex >/dev/null 2>&1 && ok "codex CLI $(codex --version 2>/dev/null | awk '{print $NF}')" || warn "codex CLI not installed on the host (only setup's judge-model probe needs it; the verifier has its own)"
# every pinned base image the harness builds from must exist for this platform; the
# ECR variants (Dockerfile.ecr) are linux/amd64 only and are not checked here
for df in "$HERE/templates/tests/Dockerfile" "$HERE/vendor/light-servers/Dockerfile"; do
    for ref in $(sed -n 's/^FROM \([^ ]*@sha256:[0-9a-f]*\).*/\1/p' "$df" | sort -u); do
        # variants count (linux/arm64/v8 is linux/arm64); attestation entries (unknown/unknown) do not
        if plats=$(docker buildx imagetools inspect "$ref" 2>/dev/null | sed -n 's/^ *Platform: *//p' | grep -v '^unknown'); then
            if printf '%s\n' "$plats" | grep -qE "^${PLATFORM}(/|$)"; then ok "${df#$HERE/}: ${ref%%@*} has $PLATFORM"
            else bad "${df#$HERE/}: ${ref%%@*} (pinned digest) has no $PLATFORM build, only: $(printf '%s ' $plats)"; fi
        else
            warn "${df#$HERE/}: could not inspect ${ref%%@*} (offline?)"
        fi
    done
done

echo "== credentials"
# The rubric judge: Rewardkit runs it through the codex CLI (ChatGPT login) or the
# claude-code CLI. The model is JUDGE_MODEL (.env) or config/models.yaml; a ChatGPT
# login refuses some models (the gpt-5.x-sol family), so the chosen one is tried here.
JUDGE_MODEL="${JUDGE_MODEL:-$(sed -n 's/^ *judge_model: *//p' "$HERE/config/models.yaml" | head -1)}"
JUDGE_TRANSPORT="${JUDGE_TRANSPORT:-$(sed -n 's/^ *judge_transport: *//p' "$HERE/config/models.yaml" | head -1)}"
if [ "${JUDGE_TRANSPORT:-codex}" = codex ]; then
    if [ -s "${CODEX_HOME:-$HOME/.codex}/auth.json" ]; then
        ok "codex login present (rubric channel will be scored)"
        if command -v codex >/dev/null 2>&1 && [ -n "$JUDGE_MODEL" ]; then
            # stdin closed: codex exec waits on an open stdin and would hang here
            if python3 - "$JUDGE_MODEL" <<'PYEOF' >/dev/null 2>&1
import subprocess, sys
r = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-m", sys.argv[1], "reply with the single word ok"],
                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
raise SystemExit(0 if r.returncode == 0 else 1)
PYEOF
            then ok "judge model $JUDGE_MODEL accepted by the codex login"
            else warn "judge model $JUDGE_MODEL refused by codex (try another JUDGE_MODEL in .env; the rubric channel would stay UNSCORED)"
            fi
        elif [ -z "$JUDGE_MODEL" ]; then
            warn "no judge model: set JUDGE_MODEL in .env or defaults.judge_model in config/models.yaml"
        fi
    else
        warn "no ~/.codex/auth.json: the rubric channel stays UNSCORED; run 'codex login' (or 'codex login --device-auth' headless)"
    fi
elif [ "$JUDGE_TRANSPORT" = claude-code ]; then
    [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && ok "claude-code judge: CLAUDE_CODE_OAUTH_TOKEN set" \
        || warn "JUDGE_TRANSPORT=claude-code needs CLAUDE_CODE_OAUTH_TOKEN in .env (claude setup-token)"
else
    warn "JUDGE_TRANSPORT=$JUDGE_TRANSPORT is not codex or claude-code"
fi
[ -s "$HERE/vendor/zbridge/.env" ] && ok "zbridge .env present" || warn "vendor/zbridge/.env missing (GLM route)"
[ -s "$HERE/vendor/bbridge/.env" ] && ok "bbridge .env present" || warn "vendor/bbridge/.env missing (glm-5.3-bedrock route)"
# the claude CLI keeps its login in ~/.claude/.credentials.json on Linux and in the
# Keychain on macOS; the bridge reads either
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    ok "claude login: CLAUDE_CODE_OAUTH_TOKEN (token login, works headless on either OS)"
elif [ -s "$HOME/.claude/.credentials.json" ]; then
    ok "claude login: ~/.claude/.credentials.json"
elif command -v security >/dev/null 2>&1 && security find-generic-password -s "Claude Code-credentials" >/dev/null 2>&1; then
    ok "claude login: macOS Keychain (set AURORA_CC_WRITE_BACK_KEYCHAIN=1 in .env so refreshes are written back)"
elif false; then
    :
else
    warn "no Claude login found (opus-5 route): run 'claude' once and log in"
fi

[ "$CHECK_ONLY" = 1 ] && { echo; [ "$fail" = 0 ] && echo "ready" || echo "missing prerequisites above"; exit $fail; }
[ "$fail" = 0 ] || { echo; echo "fix the MISS lines above, then re-run"; exit 1; }

echo "== harbor venv"
# vendor/ carries exactly one harbor tree; the venv must hold that version. An
# older install (or none) is rebuilt, so bumping the vendored tree is one re-run.
HARBOR_SRC=$(ls -d "$HERE"/vendor/harbor-*/ | head -1); HARBOR_SRC=${HARBOR_SRC%/}
WANT=$(sed -n 's/^version = "\(.*\)"/\1/p' "$HARBOR_SRC/pyproject.toml")
HAVE=""; [ -x "$HERE/.venv/bin/harbor" ] && HAVE=$("$HERE/.venv/bin/harbor" --version 2>/dev/null || true)
if [ -n "$HAVE" ] && [ "$HAVE" = "$WANT" ]; then
    ok "harbor $HAVE"
else
    [ -n "$HAVE" ] && echo "   harbor $HAVE installed, ${HARBOR_SRC#$HERE/} carries $WANT: rebuilding .venv"
    rm -rf "$HERE/.venv"
    uv venv "$HERE/.venv" --python 3.12 >/dev/null
    uv pip install --python "$HERE/.venv/bin/python" -e "$HARBOR_SRC" -q \
        && ok "harbor $WANT installed from ${HARBOR_SRC#$HERE/}" || bad "harbor install failed"
    uv pip install --python "$HERE/.venv/bin/python" pyyaml -q
fi

echo "== bridges"
if [ -d "$HERE/vendor/zbridge" ]; then
    if [ ! -x "$HERE/vendor/zbridge/.venv/bin/python" ]; then
        uv venv "$HERE/vendor/zbridge/.venv" --python 3.12 >/dev/null
        (cd "$HERE/vendor/zbridge" && uv pip install --python .venv/bin/python -e . -q) \
            && ok "zbridge venv built" || bad "zbridge install failed"
    else ok "zbridge venv present"; fi
    # vendor/zbridge/.env is derived, never hand-edited: the key comes from the
    # top-level .env, the two secrets are generated once and then left alone.
    ZENV="$HERE/vendor/zbridge/.env"; umask 077; touch "$ZENV"
    zb_get() { grep "^$1=" "$ZENV" 2>/dev/null | head -1 | cut -d= -f2-; }
    zb_set() { local k=$1 v=$2; if grep -q "^$k=" "$ZENV" 2>/dev/null
               then sed "s|^$k=.*|$k=$v|" "$ZENV" > "$ZENV.tmp" && mv "$ZENV.tmp" "$ZENV"
               else echo "$k=$v" >> "$ZENV"; fi; }
    rand() { head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9'; }

    [ -n "$(zb_get ZB_BRIDGE_SECRET)" ]    || zb_set ZB_BRIDGE_SECRET "zb-$(rand)"
    [ -n "$(zb_get ZB_THINKING_SIG_KEY)" ] || zb_set ZB_THINKING_SIG_KEY "$(rand)"
    if [ -n "${ZB_ZAI_API_KEY:-}" ]; then
        [ "$(zb_get ZB_ZAI_API_KEY)" = "$ZB_ZAI_API_KEY" ] || { zb_set ZB_ZAI_API_KEY "$ZB_ZAI_API_KEY"
            ok "pushed ZB_ZAI_API_KEY from .env into vendor/zbridge/.env"; }
    elif [ -z "$(zb_get ZB_ZAI_API_KEY)" ]; then
        warn "no ZB_ZAI_API_KEY in .env: the GLM routes will not authenticate"
    fi
    chmod 600 "$ZENV"
fi
if [ -d "$HERE/vendor/bbridge" ]; then
    if [ ! -x "$HERE/vendor/bbridge/.venv/bin/python" ]; then
        uv venv "$HERE/vendor/bbridge/.venv" --python 3.12 >/dev/null
        (cd "$HERE/vendor/bbridge" && uv pip install --python .venv/bin/python -e . -q) \
            && ok "bbridge venv built" || bad "bbridge install failed"
    else ok "bbridge venv present"; fi
    # vendor/bbridge/.env is derived like zbridge's: key, ARN and region come from the
    # top-level .env, the two secrets are generated once and then left alone.
    BENV="$HERE/vendor/bbridge/.env"; umask 077; touch "$BENV"
    bb_get() { grep "^$1=" "$BENV" 2>/dev/null | head -1 | cut -d= -f2-; }
    bb_set() { local k=$1 v=$2; if grep -q "^$k=" "$BENV" 2>/dev/null
               then sed "s|^$k=.*|$k=$v|" "$BENV" > "$BENV.tmp" && mv "$BENV.tmp" "$BENV"
               else echo "$k=$v" >> "$BENV"; fi; }
    rand() { head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9'; }

    [ -n "$(bb_get BB_BRIDGE_SECRET)" ]    || bb_set BB_BRIDGE_SECRET "bb-$(rand)"
    [ -n "$(bb_get BB_THINKING_SIG_KEY)" ] || bb_set BB_THINKING_SIG_KEY "$(rand)"
    [ -n "${BEDROCK_GLM_ARN:-}" ] && [ "$(bb_get BB_MODEL_ARN)" != "$BEDROCK_GLM_ARN" ] && bb_set BB_MODEL_ARN "$BEDROCK_GLM_ARN"
    [ -n "${BEDROCK_REGION:-}" ]  && [ "$(bb_get BB_REGION)" != "$BEDROCK_REGION" ]  && bb_set BB_REGION "$BEDROCK_REGION"
    if [ -n "${BEDROCK_API_KEY:-}" ]; then
        [ "$(bb_get BB_BEDROCK_API_KEY)" = "$BEDROCK_API_KEY" ] || { bb_set BB_BEDROCK_API_KEY "$BEDROCK_API_KEY"
            ok "pushed BEDROCK_API_KEY from .env into vendor/bbridge/.env"; }
    elif [ -z "$(bb_get BB_BEDROCK_API_KEY)" ]; then
        warn "no BEDROCK_API_KEY in .env: the glm-5.3-bedrock route will not authenticate"
    fi
    chmod 600 "$BENV"
fi
if [ -d "$HERE/vendor/harness-levi-bridge" ]; then
    if [ ! -x "$HERE/vendor/harness-levi-bridge/.venv/bin/python" ]; then
        uv venv "$HERE/vendor/harness-levi-bridge/.venv" --python 3.12 >/dev/null
        uv pip install --python "$HERE/vendor/harness-levi-bridge/.venv/bin/python" \
            -r "$HERE/vendor/harness-levi-bridge/requirements.txt" -q \
            && ok "claude bridge venv built" || warn "claude bridge install failed"
    else ok "claude bridge venv present"; fi
    # the launcher finds its python in this folder's .venv; `check` reads the Claude
    # login (Keychain on macOS, ~/.claude/.credentials.json on Linux) without starting
    if "$HERE/vendor/harness-levi-bridge/claude_code_bridge.sh" check >/dev/null 2>&1; then
        ok "claude bridge sees a valid Claude login"
    else
        warn "claude bridge cannot read a Claude login: run 'claude' and log in, then './vendor/harness-levi-bridge/claude_code_bridge.sh check'"
    fi
fi

echo
[ "$fail" = 0 ] && echo "setup complete: ./run.sh --task input/<task> --model <model>" \
               || echo "setup finished with problems above"
exit $fail
