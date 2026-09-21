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
avail=$(df -BG --output=avail "$HERE" | tail -1 | tr -dc '0-9')
[ "${avail:-0}" -ge 40 ] && ok "disk ${avail}G free" || warn "only ${avail}G free; 40G+ recommended"
for port in 8765 8766; do
    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$port "; then
        warn "port $port already in use (a bridge may already be running)"
    else
        ok "port $port free"
    fi
done

echo "== credentials"
[ -s "${CODEX_HOME:-$HOME/.codex}/auth.json" ] \
    && ok "codex login present (rubric channel will be scored)" \
    || warn "no ~/.codex/auth.json: the rubric channel stays UNSCORED; run 'codex login'"
[ -s "$HERE/vendor/zbridge/.env" ] && ok "zbridge .env present" || warn "vendor/zbridge/.env missing (GLM route)"
[ -s "$HOME/.claude/.credentials.json" ] || [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
    && ok "claude credentials present" || warn "no Claude login found (opus-5 route)"

[ "$CHECK_ONLY" = 1 ] && { echo; [ "$fail" = 0 ] && echo "ready" || echo "missing prerequisites above"; exit $fail; }
[ "$fail" = 0 ] || { echo; echo "fix the MISS lines above, then re-run"; exit 1; }

echo "== harbor venv"
if [ -x "$HERE/.venv/bin/harbor" ]; then
    ok "harbor $("$HERE/.venv/bin/harbor" --version 2>/dev/null)"
else
    uv venv "$HERE/.venv" --python 3.12 >/dev/null
    uv pip install --python "$HERE/.venv/bin/python" -e "$HERE/vendor/harbor-0.23.0" -q \
        && ok "harbor installed from vendor/harbor-0.23.0" || bad "harbor install failed"
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
               then sed -i "s|^$k=.*|$k=$v|" "$ZENV"; else echo "$k=$v" >> "$ZENV"; fi; }
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
if [ -d "$HERE/vendor/harness-levi-bridge" ]; then
    if [ ! -x "$HERE/vendor/harness-levi-bridge/.venv/bin/python" ]; then
        uv venv "$HERE/vendor/harness-levi-bridge/.venv" --python 3.12 >/dev/null
        uv pip install --python "$HERE/vendor/harness-levi-bridge/.venv/bin/python" \
            -r "$HERE/vendor/harness-levi-bridge/requirements.txt" -q \
            && ok "claude bridge venv built" || warn "claude bridge install failed"
    else ok "claude bridge venv present"; fi
fi

echo
[ "$fail" = 0 ] && echo "setup complete: ./run.sh --task input/<task> --model <model>" \
               || echo "setup finished with problems above"
exit $fail
