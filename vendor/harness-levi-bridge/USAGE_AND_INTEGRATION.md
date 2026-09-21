# Claude Code Bridge — Usage & Integration

A small Anthropic-compatible HTTP proxy that lets the LEVI harness (or any
LiteLLM/Anthropic-SDK client) run on a **Claude Code Pro/Max subscription**
via OAuth, instead of a metered `ANTHROPIC_API_KEY`.

You start it on the host; it listens on `:8765`; you point your LLM config's
`base_url` at it. Nothing else in your client changes.

---

## Table of contents

1. [What's in this bundle](#1-whats-in-this-bundle)
2. [How it works](#2-how-it-works)
3. [Prerequisites](#3-prerequisites)
4. [Install (standalone)](#4-install-standalone)
5. [Install (inside the LEVI harness)](#5-install-inside-the-levi-harness)
6. [Start / stop / status](#6-start--stop--status)
7. [Point your client at the bridge](#7-point-your-client-at-the-bridge)
8. [Docker networking — the part that usually breaks](#8-docker-networking--the-part-that-usually-breaks)
9. [Credential sources & the refresh-token footgun](#9-credential-sources--the-refresh-token-footgun)
10. [Multi-account pool](#10-multi-account-pool)
11. [Environment variable reference](#11-environment-variable-reference)
12. [Endpoints](#12-endpoints)
13. [Error handling & retry semantics](#13-error-handling--retry-semantics)
14. [Verifying it works](#14-verifying-it-works)
15. [Troubleshooting](#15-troubleshooting)
16. [Integrating into a different codebase](#16-integrating-into-a-different-codebase)
17. [Security & terms](#17-security--terms)

---

## 1. What's in this bundle

```
harness-levi-bridge/
├── claude_code_bridge/            # the proxy — standalone Python package (relative imports only)
│   ├── __init__.py                #   public exports
│   ├── __main__.py                #   CLI entry: python -m claude_code_bridge
│   ├── bridge.py                  #   FastAPI app: forwarding, header rewrite, streaming, retry/failover
│   ├── credentials.py             #   OAuth load / refresh / cache / keychain + multi-account pool
│   └── errors.py                  #   Anthropic error classification
├── claude_code_bridge.sh          # start | stop | status | check | monitor  (background + watchdog)
├── claude-code-oauth.json         # the LLM config you hand to the harness
├── requirements.txt               # fastapi, uvicorn, httpx — nothing else
├── USAGE_AND_INTEGRATION.md       # this file
└── TRAJECTORY_GENERATION.md       # how bridge + harness + OpenHands produce trajectories
```

The package has **no `pyproject.toml`** on purpose: it is imported by path, not
installed. Run it from the directory that *contains* `claude_code_bridge/`.

Auth tests for this bridge live in the harness repo at
`harness/benchmarks/multiswebench/tests/test_claude_bridge_auth.py` and are not
shipped here.

---

## 2. How it works

```
your client (OpenHands SDK / LiteLLM / anthropic-sdk / curl)
  └─ POST {base_url}/v1/messages     headers: x-api-key: sk-ant-oauth-bridge-stub
      └─ [ claude_code_bridge on the host, :8765 ]
           • strips   x-api-key / authorization / host / content-length / accept-encoding
           • adds     Authorization: Bearer <your Claude Code OAuth access token>
           • adds     anthropic-beta: oauth-2025-04-20   (merged with yours, never replaced)
           • adds     anthropic-version: 2023-06-01      (only if you didn't send one)
           • injects  "You are Claude Code, Anthropic's official CLI for Claude."
                      as a system prefix on POST /v1/messages   (idempotent)
           • refreshes the OAuth token proactively 60s before expiry
           • classifies upstream errors, retries transient ones, fails over accounts
           └─ POST https://api.anthropic.com/v1/messages   ← billed to your subscription
```

Three details matter:

- **The system prefix is mandatory.** Anthropic rejects OAuth-scoped
  `/v1/messages` calls that don't begin with the Claude Code identity string.
  The bridge inserts it and handles all three shapes the Messages API accepts
  (`system` absent, `system` as a string, `system` as a list of content blocks).
  It is idempotent — if the prefix is already present, the body is untouched.
- **The stub API key is required.** LiteLLM refuses to construct an Anthropic
  client without an `api_key`. Send any placeholder; the bridge strips
  `x-api-key` before forwarding, so the value never leaves your machine.
- **Everything else is a passthrough.** The catch-all route forwards every
  method and path (`/v1/models`, `/v1/messages/count_tokens`, …), preserving
  query params, upstream status codes, and upstream headers — including
  `request-id` and the `anthropic-ratelimit-*` family — so your client keeps
  full rate-limit visibility.

---

## 3. Prerequisites

- **Python 3.10+** with `fastapi`, `uvicorn`, `httpx`.
- **A Claude Code Pro/Max subscription**, logged in at least once with the
  `claude` CLI on this machine. That login is what creates the OAuth credential
  the bridge reads. (Alternatively: a `CLAUDE_CODE_OAUTH_TOKEN` setup-token.)
- **macOS or Linux.** On macOS the credential lives in Keychain under the
  service `Claude Code-credentials`; on Linux it's `~/.claude/.credentials.json`.
- If you're driving a Dockerised agent: **`--workspace docker`, not `remote`.**
  The bridge is host-local, so a remote runtime cannot reach it.

---

## 4. Install (standalone)

```bash
unzip "harness levi bridge.zip" -d ~/tools
cd ~/tools/harness-levi-bridge

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Tell the launcher which interpreter to use — see the note below.
export AURORA_CC_PYTHON="$PWD/.venv/bin/python"

./claude_code_bridge.sh check      # validates credentials, exits
./claude_code_bridge.sh start
```

> **First thing that will fail for you.** `claude_code_bridge.sh` was written
> for the harness layout and defaults to `../.venv/bin/python` (the harness
> venv, one level up from `proxy/`). Outside that layout it will print
> `ERROR: python not found at ...`. Set `AURORA_CC_PYTHON` to your own
> interpreter and it works anywhere.

You can also skip the launcher entirely:

```bash
cd ~/tools/harness-levi-bridge      # the dir that CONTAINS claude_code_bridge/
.venv/bin/python -m claude_code_bridge --host 0.0.0.0 --port 8765
```

## 5. Install (inside the LEVI harness)

Drop the package back where it came from and the defaults just work:

```bash
cp -r claude_code_bridge claude_code_bridge.sh claude-code-oauth.json \
      /path/to/levi/harness/proxy/

cd /path/to/levi/harness
.venv/bin/python -c "import fastapi, uvicorn, httpx"   # deps already present
proxy/claude_code_bridge.sh start
```

No `AURORA_CC_PYTHON` needed — the script finds `harness/.venv/bin/python` on
its own.

> If the harness venv doesn't exist yet, build it with
> `git submodule update --init --recursive && UV_INSECURE_NO_ZIP_VALIDATION=1 uv sync --dev`.
> Prefer that over `make build` on a checkout whose `vendor/software-agent-sdk`
> pin matters — `make build` adds `--remote` and bumps the SDK submodule to
> upstream tip.

---

## 6. Start / stop / status

| Command | What it does |
|---|---|
| `./claude_code_bridge.sh check` | Load credentials, print `credentials OK`, exit. No server. |
| `./claude_code_bridge.sh start` | Background the server, write `.claude_code_bridge.pid`, poll `/healthz` for up to 10s, then start the watchdog. |
| `./claude_code_bridge.sh status` | Report bridge + watchdog PIDs and dump `/healthz`. Exit 1 if down. |
| `./claude_code_bridge.sh stop` | Kill the watchdog first (so it can't resurrect the bridge), then the bridge, then reap any orphaned listener on the port via `lsof`. |
| `./claude_code_bridge.sh monitor` | Run the watchdog in the foreground. |

Operational notes:

- **Concurrent `start` is safe.** The script takes an `flock` on
  `.claude_code_bridge.pid.lock` so two callers can't both pass the liveness
  check and race to bind the port.
- **The watchdog** polls `http://127.0.0.1:$PORT/healthz` every 30s and restarts
  the bridge after 3 consecutive failures. Tune with `AURORA_CC_MONITOR_POLL`
  and `AURORA_CC_MONITOR_FAILS`; disable with `AURORA_CC_DISABLE_MONITOR=1`.
- **Logs** land in `logs/claude_code_bridge.log` and
  `logs/claude_code_bridge_monitor.log` next to the script. PID files and logs
  are runtime artifacts — don't commit them.

---

## 7. Point your client at the bridge

`claude-code-oauth.json` is the config the harness consumes:

```json
{
  "model": "anthropic/claude-opus-4-8",
  "base_url": "http://172.17.0.1:8765",
  "api_key": "sk-ant-oauth-bridge-stub",
  "timeout": 600,
  "num_retries": 2
}
```

- `model` **must** start with `anthropic/` so LiteLLM speaks the native
  Messages API. An `openai/`-prefixed model would send Chat Completions, which
  the bridge does not translate.
- `api_key` is the throwaway stub described above.
- Adjust `base_url` per [section 8](#8-docker-networking--the-part-that-usually-breaks).

Harness invocations:

```bash
# via run_eval.sh (the normal path)
./run_eval.sh --llm-config proxy/claude-code-oauth.json \
              --dataset bundles/<bundle>.jsonl \
              --workspace docker --max-iter 1000

# direct inference entrypoint
LANGUAGE=python uv run python -m benchmarks.multiswebench.run_infer \
    proxy/claude-code-oauth.json \
    --dataset bytedance-research/Multi-SWE-Bench --split python_verified \
    --workspace docker --max-iterations 1000
```

Any other Anthropic client works too:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8765
export ANTHROPIC_API_KEY=sk-ant-oauth-bridge-stub
python -c "
import anthropic
print(anthropic.Anthropic().messages.create(
    model='claude-opus-4-8', max_tokens=64,
    messages=[{'role':'user','content':'ping'}]).content[0].text)"
```

---

## 8. Docker networking — the part that usually breaks

The agent runs **inside a container**; the bridge runs **on the host**. The
address the container uses to reach the host differs by platform:

| Where the client runs | `base_url` |
|---|---|
| Linux / EC2, container on the default bridge network | `http://172.17.0.1:8765` (docker0 gateway) |
| macOS Docker Desktop | `http://host.docker.internal:8765` |
| On the host, no Docker | `http://127.0.0.1:8765` |

Neither name works on both platforms: `172.17.x.x` doesn't exist on Docker
Desktop, and `host.docker.internal` doesn't resolve on plain Linux Docker
(containers get no `--add-host`). `run_eval.sh` handles this for you — it reads
`base_url`, detects `uname -s`, and rewrites into a **temporary** config so the
committed file stays put, logging:

```
platform(Darwin): rewrote LLM base_url http://172.17.0.1:8765 -> http://host.docker.internal:8765
```

If you invoke `run_infer.py` directly, you get no rewrite — set `base_url`
correctly yourself.

**Egress filter carve-out.** The eval container runs a mitmproxy-based egress
filter (anti-cheat). A plain-`http://` `base_url` isn't covered by its
`--ignore-hosts` SaaS allowlist and would be blocked. `run_infer.py` detects an
`http://` base URL, parses host and port, and forwards them as
`LLM_DIRECT_HOST` / `LLM_DIRECT_PORT` so the container entrypoint opens exactly
that one destination (iptables + `NO_PROXY`). This is automatic; you only need
to know it exists when debugging a container that can't reach the bridge.

The bridge therefore binds `0.0.0.0` by default (not `127.0.0.1`) so containers
can reach it. On a shared or public host, firewall port 8765 — anyone who can
reach it can spend your subscription.

---

## 9. Credential sources & the refresh-token footgun

`credentials.py` tries these in order and takes the first that yields a
credential:

| # | Source | Notes |
|---|---|---|
| 0 | `CLAUDE_CODE_OAUTH_TOKEN` env var | A Claude Code *setup-token*. Read fresh on every call, never persisted, never refreshed. Best for CI. |
| 1 | `CLAUDE_CODE_CREDENTIALS` env var | Inline JSON. For tests/CI. |
| 2 | `AURORA_CC_CREDS_PATH` env var | Path to a credentials JSON file. Explicit override. |
| 3 | macOS Keychain, service `Claude Code-credentials` | Read via `security find-generic-password`. The normal macOS path. |
| 4 | `~/.claude/.credentials.json` | The normal Linux path. |
| 5 | `~/.cache/aurora-harness/claude_creds.json` | The bridge's own refresh cache. Last resort. |

Payload shape (what the `claude` CLI stores):

```json
{"claudeAiOauth": {
   "accessToken": "sk-ant-oat01-...", "refreshToken": "sk-ant-ort01-...",
   "expiresAt": 1782402066667, "scopes": ["user:inference", "..."],
   "subscriptionType": "max"}}
```

The provider is a thread-safe lazy cache: it loads once, then refreshes
proactively when the token is within 60s of expiry, by POSTing a
`refresh_token` grant to `https://console.anthropic.com/v1/oauth/token` with
the public Claude Code client ID (3 attempts, exponential backoff; a 4xx is
raised immediately since a revoked refresh token won't recover).

> ### ⚠️ The one thing that will silently log you out of `claude`
>
> **Anthropic rotates the refresh token on every grant.** When the bridge
> refreshes, the refresh token still sitting in your Keychain becomes invalid —
> and the next `claude` CLI invocation 401s and logs you out.
>
> Three ways to avoid it, best first:
>
> 1. **Give the bridge its own dedicated Claude account** (via
>    `AURORA_CC_CREDS_PATH` or a pool slot) so it never shares a rotating grant
>    with your interactive CLI.
> 2. **`export AURORA_CC_WRITE_BACK_KEYCHAIN=1`** — the bridge pushes the
>    rotated credential back into Keychain (best-effort, macOS only), keeping
>    the CLI in sync.
> 3. **Use `CLAUDE_CODE_OAUTH_TOKEN`** — a setup-token never refreshes, so
>    nothing rotates.
>
> Without one of these, the bridge logs a loud warning on every self-refresh.
> That warning is not cosmetic.

---

## 10. Multi-account pool

Set `AURORA_CC_ACCOUNT_POOL` to a **colon-separated** list of slots. Each entry
is one of:

| Entry form | Meaning |
|---|---|
| `default` | The normal provider (Keychain → `~/.claude/.credentials.json` → cache) |
| `keychain:<service-name>` | A specific Keychain item |
| `/path/to/creds.json` | A credentials file (absolute or `~`-relative) |

```bash
export AURORA_CC_ACCOUNT_POOL="default:$HOME/.cache/aurora-harness/acc2.json:$HOME/.cache/aurora-harness/acc3.json"
./claude_code_bridge.sh start
curl -s localhost:8765/quota | python3 -m json.tool
```

With a pool active, a `subscription_cap` error marks that slot exhausted until
its reset time and the request is retried on the next healthy slot;
`oauth_token_invalid`, `account_restricted`, and `billing_error` mark the slot
invalid. `GET /quota` reports per-account state and the soonest reset, which is
what a long pipeline should poll before scheduling more work.

Attribution uses the **full** access token, not a prefix — every OAuth token
starts with `sk-ant-oat01-`, so a prefix would be ambiguous.

Note the parser splits on `:`, so a slot path containing a colon won't parse,
and `keychain:` names are taken as everything after the first colon.

---

## 11. Environment variable reference

### Launcher (`claude_code_bridge.sh`)

| Variable | Default | Purpose |
|---|---|---|
| `AURORA_CC_PYTHON` | `../.venv/bin/python` | Interpreter to run the bridge with. **Set this outside the harness layout.** |
| `AURORA_CC_BRIDGE_HOST` | `0.0.0.0` | Bind address |
| `AURORA_CC_BRIDGE_PORT` | `8765` | Bind port |
| `AURORA_CC_BRIDGE_LOG` | `logs/claude_code_bridge.log` | Server log path |
| `AURORA_CC_BRIDGE_MONITOR_LOG` | `logs/claude_code_bridge_monitor.log` | Watchdog log path |
| `AURORA_CC_DISABLE_MONITOR` | `0` | `1` skips starting the watchdog |
| `AURORA_CC_MONITOR_POLL` | `30` | Watchdog poll interval (s) |
| `AURORA_CC_MONITOR_FAILS` | `3` | Consecutive `/healthz` failures before restart |

### Credentials

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Setup-token; highest priority, no refresh |
| `CLAUDE_CODE_CREDENTIALS` | — | Inline credentials JSON |
| `AURORA_CC_CREDS_PATH` | — | Path to a credentials JSON file |
| `AURORA_CC_WRITE_BACK_KEYCHAIN` | unset | `1` writes rotated creds back to Keychain (macOS) |
| `AURORA_CC_ACCOUNT_POOL` | — | Colon-separated multi-account slot spec |

### Forwarding behaviour

| Variable | Default | Purpose |
|---|---|---|
| `AURORA_CC_UPSTREAM` | `https://api.anthropic.com` | Upstream base URL |
| `AURORA_CC_SKIP_SYSTEM_PREFIX` | unset | `1` disables system-prefix injection (**will 4xx against real OAuth upstream** — debugging only) |
| `AURORA_CC_MAX_INLINE_RETRIES` | `3` | Inline retry budget per request |
| `AURORA_CC_MAX_INLINE_WAIT` | `30` | Max seconds the bridge will sleep for one retry; longer waits are surfaced to the client instead |
| `AURORA_BRIDGE_REQUEST_TIMEOUT` | `600` | Overall upstream timeout (s) |
| `AURORA_BRIDGE_READ_TIMEOUT` | `180` | Per-chunk read timeout (s) |
| `AURORA_BRIDGE_CONNECT_TIMEOUT` | `30` | TCP connect timeout (s) |

> **Don't lower `AURORA_BRIDGE_READ_TIMEOUT`.** With extended thinking on a
> large context, a single turn can pause 90–150s between streamed chunks. The
> httpx default of 5s caused `MidStreamFallbackError` storms; 180s is the fix.

---

## 12. Endpoints

| Route | Purpose |
|---|---|
| `GET /healthz` | `{"ok": true, "token_prefix": "sk-ant-oat01-..."}`; `503` with the error string if credentials can't load. Includes `accounts` when a pool is active. |
| `GET /quota` | `{"multi_account", "accounts", "next_reset_at_unix"}` — pipeline introspection. |
| `ANY /{path}` | Catch-all passthrough to `{AURORA_CC_UPSTREAM}/{path}` for GET/POST/PUT/DELETE/PATCH. |

Streaming is detected by probing the raw body for `"stream":true`. On a
streamed request the bridge opens the upstream stream, checks the status
*before* committing, and only then hands off a chunk-passthrough generator —
so a 429 on a streaming call gets the same structured error and failover as a
non-streaming one rather than a half-open SSE stream. A false positive on the
probe is harmless.

---

## 13. Error handling & retry semantics

`errors.py` classifies every non-2xx upstream response into an `ErrorKind`:

| Kind | Retried inline? | Fails over to another account? |
|---|---|---|
| `transient_throttle` | ✅ | — |
| `overloaded` (529) | ✅ | — |
| `upstream_5xx` | ✅ | — |
| `subscription_cap` | ❌ | ✅ (slot marked exhausted until reset) |
| `oauth_token_invalid` | ❌ | ✅ (single-account: forces a credential reload) |
| `account_restricted` | ❌ | ✅ |
| `billing_error` | ❌ | ✅ |
| `invalid_request` | ❌ | ❌ (your payload is wrong) |
| `unknown` | ❌ | ❌ |

Retryable kinds are slept and retried up to `AURORA_CC_MAX_INLINE_RETRIES`,
honouring `Retry-After` when present, but only if the wait is within
`AURORA_CC_MAX_INLINE_WAIT`. A longer wait is handed back to the client so the
*caller* can decide — the bridge will not silently block a request for an hour
on a subscription cap.

Errors reach the client as a normal Anthropic error envelope plus an
`aurora_bridge` block:

```json
{"type": "error",
 "error": {"type": "rate_limit_error", "message": "..."},
 "aurora_bridge": {"kind": "subscription_cap",
                   "retry_after_seconds": 1800,
                   "reset_at_unix": 1782402066,
                   "request_id": "req_..."}}
```

with headers `X-Aurora-Bridge-Error: <kind>`, `Retry-After`, and
`X-Aurora-Reset-At`. Grepping run logs for `X-Aurora-Bridge-Error` or
`"kind":` is the fastest way to tell "we hit the cap" from "the model
misbehaved". Credential failures return `401` with
`{"aurora_bridge": {"kind": "credentials_unavailable"}}`; network failures
return `502` with `kind: network_error`.

Credential loading (Keychain subprocess, a synchronous refresh, a blocking
`flock`) runs via `asyncio.to_thread`, so one refresh can't freeze every
concurrent request on the event loop.

---

## 14. Verifying it works

```bash
# 1. credentials load
./claude_code_bridge.sh check
#    -> [bridge] credentials OK

# 2. server is healthy
curl -s localhost:8765/healthz | python3 -m json.tool
#    -> {"ok": true, "token_prefix": "sk-ant-oat01-..."}

# 3. a real round-trip on your subscription
curl -s localhost:8765/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-api-key: sk-ant-oauth-bridge-stub' \
  -d '{"model":"claude-opus-4-8","max_tokens":32,
       "messages":[{"role":"user","content":"reply with the word ok"}]}' \
  | python3 -m json.tool

# 4. from inside a container (Linux)
docker run --rm curlimages/curl -s http://172.17.0.1:8765/healthz
# macOS Docker Desktop:
docker run --rm curlimages/curl -s http://host.docker.internal:8765/healthz
```

Step 4 is the one worth doing before a long run — steps 1–3 pass on a host
whose container networking is still broken.

---

## 15. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ERROR: python not found at .../.venv/bin/python` | Launcher's harness-relative default | `export AURORA_CC_PYTHON=/path/to/python` |
| `ModuleNotFoundError: claude_code_bridge` | Running from the wrong cwd | `cd` into the dir *containing* `claude_code_bridge/`; the launcher does this for you |
| `[bridge] credentials error: ...` | No Claude Code login on this machine | Run `claude` and log in, or set `CLAUDE_CODE_OAUTH_TOKEN` |
| `claude` CLI suddenly logs out | Bridge self-refresh rotated the shared refresh token | See [section 9](#9-credential-sources--the-refresh-token-footgun) |
| Container: connection refused / timeout to the bridge | Wrong host address, or bridge bound to `127.0.0.1` | Use the platform address from [section 8](#8-docker-networking--the-part-that-usually-breaks); keep `HOST=0.0.0.0` |
| Container reaches nothing at all, mitmproxy in the logs | Egress filter blocking the plain-HTTP LLM endpoint | Confirm `LLM_DIRECT_HOST`/`LLM_DIRECT_PORT` were forwarded; `EGRESS_FILTER_DISABLE=1` as an escape hatch |
| `MidStreamFallbackError`, repeated mid-stream drops | Read timeout too low for extended thinking | Leave `AURORA_BRIDGE_READ_TIMEOUT` at 180 or raise it |
| 4xx mentioning the system prompt | `AURORA_CC_SKIP_SYSTEM_PREFIX=1` is set | Unset it — OAuth requires the prefix |
| `X-Aurora-Bridge-Error: subscription_cap` | Subscription window exhausted | Wait for `X-Aurora-Reset-At`, or configure a pool ([section 10](#10-multi-account-pool)) |
| LiteLLM sends Chat Completions, upstream 404s | `model` not prefixed `anthropic/` | Fix the config's `model` field |
| Port 8765 busy after a crash | Stale listener | `./claude_code_bridge.sh stop` (it reaps orphans via `lsof`) |

Logs: `logs/claude_code_bridge.log` (start with `tail -50`) and
`logs/claude_code_bridge_monitor.log` for restart history.

---

## 16. Integrating into a different codebase

The package depends on nothing in the harness — only `fastapi`, `uvicorn`,
`httpx`, and the stdlib. All intra-package imports are relative.

**As a subprocess (recommended).** Copy the folder, `pip install -r
requirements.txt`, run `python -m claude_code_bridge --host 0.0.0.0 --port
8765`, and point your client's `base_url` at it. Health-gate your pipeline on
`GET /healthz` before submitting work.

**As an ASGI app.** `bridge.py` exposes a module-level `app`, so
`uvicorn claude_code_bridge.bridge:app` works, as does mounting it inside a
larger FastAPI application. For an explicit provider:

```python
from claude_code_bridge import CredentialProvider
from claude_code_bridge.bridge import build_app

app = build_app(CredentialProvider())
```

`build_app(provider=None)` resolves the provider from `AURORA_CC_ACCOUNT_POOL`
when you don't pass one.

**As a library.** `load_credentials()`, `refresh_credentials()`,
`load_account_pool()`, `classify_anthropic_error()`, and `ErrorKind` are all
exported from the package root if you only want the credential or
classification logic.

Two integration rules worth stating plainly:

1. **One bridge per host, not per worker.** The provider caches and refreshes a
   single credential under a lock; N processes racing to refresh the same
   rotating grant is exactly the scenario that invalidates it. Run one bridge
   and let every worker share it.
2. **Model IDs pass through untouched.** The bridge does not rewrite `model`.
   Whatever your subscription can serve is what you can request.

---

## 17. Security & terms

- **No credential ever leaves your machine** except to `api.anthropic.com` and
  `console.anthropic.com`. The bridge does not log full tokens — only a
  15–20 character prefix in `/healthz` and debug lines.
- **Binding `0.0.0.0` is deliberate** (containers must reach it) and is a real
  exposure on a shared host. There is no authentication on the bridge: anyone
  who can reach port 8765 can spend your subscription. Firewall it.
- **Don't commit runtime artifacts** — `*.pid`, `*.pid.lock`, `logs/`,
  `__pycache__/`. Log files can contain token prefixes and request IDs.
- **`claude-code-oauth.json` contains no secret** — only the stub key. It is
  safe to commit; the real credential is read from Keychain/disk at runtime.
- **Usage is billed to your Claude subscription** and is subject to Anthropic's
  Terms of Service and usage policies. Review them before pointing sustained
  automated load at a personal subscription; caps apply per account, which is
  precisely what `/quota` and the pool mechanism exist to manage.
