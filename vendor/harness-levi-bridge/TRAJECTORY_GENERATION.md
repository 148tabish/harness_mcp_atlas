# How the LEVI Harness + OpenHands + the Claude Code Bridge Produce Trajectories

A complete walkthrough of what happens between typing `./run_eval.sh` and
getting an `output.jsonl` full of agent trajectories — where each component
sits, what it hands to the next one, where the bridge intercepts, and which
files on disk hold the result.

Reference points into the harness repo (`EtharaOrion/levi-harness`) are given
as `path:symbol` so you can read the code alongside this.

---

## Table of contents

1. [Vocabulary: what a "trajectory" is here](#1-vocabulary-what-a-trajectory-is-here)
2. [The five layers](#2-the-five-layers)
3. [The full request path, end to end](#3-the-full-request-path-end-to-end)
4. [Stage 1 — `run_eval.sh`: the outer orchestrator](#4-stage-1--run_evalsh-the-outer-orchestrator)
5. [Stage 2 — `run_infer.py`: building the evaluation](#5-stage-2--run_inferpy-building-the-evaluation)
6. [Stage 3 — the workspace: a container per instance](#6-stage-3--the-workspace-a-container-per-instance)
7. [Stage 4 — the agent and the conversation loop](#7-stage-4--the-agent-and-the-conversation-loop)
8. [Stage 5 — every LLM call crosses the bridge](#8-stage-5--every-llm-call-crosses-the-bridge)
9. [Stage 6 — harvesting the trajectory](#9-stage-6--harvesting-the-trajectory)
10. [Stage 7 — persistence and the on-disk layout](#10-stage-7--persistence-and-the-on-disk-layout)
11. [What one trajectory record actually contains](#11-what-one-trajectory-record-actually-contains)
12. [Failure paths and partial capture](#12-failure-paths-and-partial-capture)
13. [How the bridge shapes trajectory quality](#13-how-the-bridge-shapes-trajectory-quality)
14. [Traps that silently void a run](#14-traps-that-silently-void-a-run)
15. [Reproducing a single trajectory by hand](#15-reproducing-a-single-trajectory-by-hand)
16. [Appendix A — component/file map](#appendix-a--componentfile-map)
17. [Appendix B — the OAuth paths, and which one is live](#appendix-b--the-oauth-paths-and-which-one-is-live)

---

## 1. Vocabulary: what a "trajectory" is here

A **trajectory** is the complete, ordered event history of one agent attempting
one task instance: every model message, every tool call (the *action*), every
tool result (the *observation*), the token/cost metrics, and the final `git
diff` the agent produced.

Concretely it is one JSON line in `output.jsonl`, deserialisable into
`benchmarks/utils/models.py:EvalOutput`, whose `history` field is a
`list[Event]` of OpenHands SDK events.

Three things it is **not**:

- It is not a chat log. Actions and observations are typed, structured SDK
  events (`ActionEvent`, `ObservationEvent`, `MessageEvent`), not prose.
- It is not the score. Scoring and judging are a separate downstream phase that
  *consumes* trajectories; see `harness/assay/` and `RUBRIC.md`.
- It is not per-run. One `run_eval.sh` invocation with `--k N` produces N runs,
  each a full pass over the dataset, each with its own `output.jsonl`.

---

## 2. The five layers

```
┌───────────────────────────────────────────────────────────────────────┐
│ 1. run_eval.sh                       HOST — bash orchestrator          │
│    dataset staging · image builds · pass@k loop · config rewriting ·   │
│    inference → verification → rubric phases · timing receipts          │
└───────────────┬───────────────────────────────────────────────────────┘
                │ spawns, once per run
┌───────────────▼───────────────────────────────────────────────────────┐
│ 2. benchmarks.multiswebench.run_infer   HOST — python orchestrator     │
│    loads LLM config · builds EvalMetadata · prepares dataset ·         │
│    Evaluation.run() fans out N workers, one per instance               │
└───────────────┬───────────────────────────────────────────────────────┘
                │ one per instance
┌───────────────▼───────────────────────────────────────────────────────┐
│ 3. DockerWorkspace → agent-server container   CONTAINER                │
│    the repo under test at /workspace/<repo>/ · bash · file editor ·    │
│    mitmproxy egress filter (anti-cheat)                                │
└───────────────┬───────────────────────────────────────────────────────┘
                │ agent loop; LLM calls exit the container
┌───────────────▼───────────────────────────────────────────────────────┐
│ 4. OpenHands SDK Agent + Conversation   runs INSIDE the container      │
│    system prompt · tool schemas · think → act → observe, ≤ max_iter    │
│    each turn → litellm → POST {base_url}/v1/messages                   │
└───────────────┬───────────────────────────────────────────────────────┘
                │ plain HTTP to the docker gateway
┌───────────────▼───────────────────────────────────────────────────────┐
│ 5. claude_code_bridge   HOST, :8765                                    │
│    OAuth injection · system prefix · retry · failover                  │
│    → https://api.anthropic.com/v1/messages  (billed to subscription)   │
└───────────────────────────────────────────────────────────────────────┘
```

The single most important structural fact: **the agent runs inside a container,
the bridge runs on the host.** Everything about networking, the egress
carve-out, and why `--workspace remote` is unsupported follows from that one
boundary.

---

## 3. The full request path, end to end

One agent turn, traced through every hop:

```
Agent decides to call a tool / produce a message
 └─ openhands.sdk Agent.step()  →  LLM.completion(messages, tools)
     └─ litellm, provider "anthropic/"   (model="anthropic/claude-opus-4-8")
         └─ POST http://172.17.0.1:8765/v1/messages
            headers: x-api-key: sk-ant-oauth-bridge-stub
                     anthropic-version: 2023-06-01
            body:    {"model": "claude-opus-4-8", "system": [...],
                      "messages": [...], "tools": [...], "stream": true}
             │
             │   leaves the container via the docker0 gateway.
             │   The in-container mitmproxy egress filter would normally block a
             │   plain-http:// destination; run_infer forwards LLM_DIRECT_HOST /
             │   LLM_DIRECT_PORT so the entrypoint opens exactly this one route
             │   (iptables + NO_PROXY). Everything else stays filtered.
             ▼
        [ claude_code_bridge on the host ]
          1. reads raw body, detects  "stream":true
          2. normalises path → "v1/messages"
          3. inject_system_prefix(): prepends
             "You are Claude Code, Anthropic's official CLI for Claude."
             (idempotent; handles system-absent / string / block-list)
          4. _build_forward_headers():
               strip  host, authorization, x-api-key, content-length,
                      connection, accept-encoding, proxy-authorization
               add    Authorization: Bearer <OAuth access token>
               merge  anthropic-beta: oauth-2025-04-20[,<caller betas>]
               add    anthropic-version: 2023-06-01  (only if absent)
          5. get_access_token() via asyncio.to_thread
               → Keychain / creds file / env / cache
               → refresh if within 60s of expiry (rotates the refresh token!)
          6. _stream_with_failover(): open upstream, check status FIRST
             ▼
        POST https://api.anthropic.com/v1/messages
             │
             ├─ 2xx → forward upstream status + headers (request-id,
             │        anthropic-ratelimit-*) and stream chunks back verbatim
             │
             └─ non-2xx → drain ≤64KB, classify_anthropic_error()
                  transient_throttle / overloaded / upstream_5xx
                      → sleep (honouring Retry-After, capped at 30s) and retry
                  subscription_cap / oauth_token_invalid / account_restricted
                  / billing_error
                      → mark the account, fail over to the next pool slot
                      → or return a structured `aurora_bridge` error envelope
             ▼
        chunks flow back → litellm assembles → SDK parses tool calls
 └─ Conversation emits ActionEvent → executes the tool in the container
     → emits ObservationEvent → next iteration
```

Every one of those events lands in `conversation.state.events`, which *is* the
trajectory.

---

## 4. Stage 1 — `run_eval.sh`: the outer orchestrator

`harness/run_eval.sh` (~1500 lines of bash) is the normal entry point:

```bash
./run_eval.sh --llm-config proxy/claude-code-oauth.json \
              --dataset bundles/<bundle>.jsonl \
              --workspace docker \
              --max-iter 1000
```

What it does before any agent runs:

1. **Validates arguments.** `--llm-config` is mandatory; `--max-iter` defaults
   to **1000** (this default matters — see [section 14](#14-traps-that-silently-void-a-run)).
2. **Pins the SDK sha** the same way `run_infer` does, via
   `benchmarks.utils.version`, so the image tag and the recorded metadata agree.
3. **Builds or pulls agent-server images**, logging into ECR if an
   `--ecr-prefix` is given.
4. **Rewrites the bridge `base_url` for the platform.** A committed config can
   only encode one platform's host name, and both directions break: `172.17.x.x`
   doesn't exist on macOS Docker Desktop, and `host.docker.internal` doesn't
   resolve on plain Linux Docker. So `run_eval.sh` reads `base_url`, checks
   `uname -s`, and if they mismatch writes a **temporary** config via
   `mk_temp_llm_config` — the committed file is never mutated:

   ```
   platform(Darwin): rewrote LLM base_url http://172.17.0.1:8765
                     -> http://host.docker.internal:8765 (temp config: /tmp/llm_config_rewrite.XXXX)
   ```

   Both `LLM_CONFIG` and `RUNTIME_LLM_CONFIG` are reassigned so the compression
   upstream and the `metadata.json` sidecar also see the reachable URL.
5. **Stages the dataset** into the canonical data dir under a unique basename.
6. **Runs the pass@k loop.** For `i` in `1..K`, creating `run_${i}/`, skipping
   any run that already has `output.report.json` unless `--force`.

For each run it invokes inference:

```bash
uv run python -m benchmarks.multiswebench.run_infer \
    "$RUNTIME_LLM_CONFIG" --dataset "$CANONICAL_DATASET" --split "$SPLIT" \
    --lang "$LANG" --workspace "$WORKSPACE" --max-iterations "$MAX_ITER" \
    --conversation-timeout "$CONVERSATION_TIMEOUT" \
    --num-workers "$NUM_WORKERS" --max-retries "$MAX_RETRIES" \
    --max-attempts 1 --output-dir "$RUN_DIR"
```

stdout/stderr go to `run_${i}/infer.log`. Phase timestamps
(`PH_EXEC_START/END`, `PH_VERIF_START/END`) are recorded around each phase to
produce the timing receipts the audit trail depends on.

After inference come the **verification** phase (apply each `git_patch`, run the
instance's tests) and optionally the **rubric** phase (an LLM judge council
reading `RUBRIC_LLM_CONFIG`, default `.llm_config/rubric-judge.json`). Those
consume trajectories; they do not create them.

---

## 5. Stage 2 — `run_infer.py`: building the evaluation

`benchmarks/multiswebench/run_infer.py:main()` does six things in order.

**1. Load the LLM config.**

```python
llm = load_inference_llm(Path(llm_config_path), no_retries=args.no_llm_retries)
```

`load_inference_llm` is just `LLM.model_validate_json(path.read_text())` plus an
optional retry-policy override. So `claude-code-oauth.json` is deserialised
straight into the SDK's `LLM` object:

```json
{"model": "anthropic/claude-opus-4-8",
 "base_url": "http://172.17.0.1:8765",
 "api_key": "sk-ant-oauth-bridge-stub",
 "timeout": 600, "num_retries": 2}
```

This is the **only** place the bridge enters the picture from the harness's
side. Everything downstream just sees an `LLM` with a `base_url`. The
`anthropic/` prefix is load-bearing: it makes litellm speak the native Messages
API, which is the only shape the bridge forwards. `--no-llm-retries` sets
`num_retries = 1` (tenacity's `stop_after_attempt`, so one *total* attempt) —
used by strict smoke runs that want a bridge failure to surface rather than be
papered over.

**2. Construct the output directory.**

```
<output-dir>/<dataset>__<split>/<model>_sdk_<SDK_SHORT_SHA>_maxiter_<N>[_N_<note>]/
```

The SDK sha and the iteration ceiling are baked into the *path*, so two runs
with different ceilings can never overwrite each other.

**3. Create the critic** (`benchmarks/utils/critics.py:create_critic`). In
iterative mode the critic decides whether an attempt counts as successful and
whether to try again. `run_eval.sh` passes `--max-attempts 1`, so in the normal
pipeline the critic is a pass-through.

**4. Build `MultiSWEBenchEvalMetadata`** — the immutable description of the run:
the `LLM`, dataset/split/lang, `max_iterations`, `conversation_timeout`
(default 3600s per `conversation.run` call, independent of iteration count),
output dir, prompt template path, `env_setup_commands`
(`export PIP_CACHE_DIR=~/.cache/pip`), `max_attempts`, critic,
`workspace_type`, `enable_delegation`.

**5. Instantiate `MultiSWEBenchEvaluation(metadata, num_workers)`** — a subclass
of the abstract orchestrator in `benchmarks/utils/evaluation.py` which
implements three hooks: `prepare_instances()`, `prepare_workspace(instance)`,
and `evaluate_instance(instance, workspace)`.

**6. Run.**

```python
evaluator.run(on_result=get_default_on_result_writer(evaluator.output_path))
```

`Evaluation.run()` fans out `num_workers` processes. Each pulls an instance,
prepares a workspace, runs `evaluate_instance`, and passes the resulting
`EvalOutput` to the writer callback.

---

## 6. Stage 3 — the workspace: a container per instance

`prepare_workspace()` builds a `DockerWorkspace` (the `remote` branch exists but
is **incompatible with the host-local bridge** — a remote runtime can't reach
`127.0.0.1` or the docker0 gateway on your machine).

What gets wired into the container:

| Concern | Mechanism |
|---|---|
| Agent server image | `EVAL_AGENT_SERVER_IMAGE:<sdk_sha>-<custom_tag><suffix>`, built or pulled; the build is verified to actually contain the expected tag |
| Platform | `DOCKER_PLATFORM`, else auto-detected (`linux/arm64` on Apple silicon, else `linux/amd64`) |
| Working dir | `/workspace` |
| Vertex SA (optional) | `VERTEX_SA_HOST_PATH` bind-mounts the key; `forward_env` carries `GOOGLE_APPLICATION_CREDENTIALS` etc. Irrelevant on the bridge path |
| Egress escape hatch | `EGRESS_FILTER_DISABLE` forwarded when set |
| **LLM carve-out** | If `base_url` starts with `http://`, its hostname/port are exported as `LLM_DIRECT_HOST`/`LLM_DIRECT_PORT` and forwarded |

That last row is the bridge's lifeline. The container runs a mitmproxy-based
**egress filter** — the anti-cheat measure that stops an agent from fetching the
upstream fix off the internet. When it can't bind, the entrypoint still exports
`HTTPS_PROXY` pointing at a dead proxy, which blocks *all* egress. An `https://`
LLM endpoint is covered by the filter's `--ignore-hosts` SaaS allowlist, but the
bridge is plain `http://` on a private IP and is not. So `run_infer` parses the
URL and tells the entrypoint to open exactly that one host:port directly. The
anti-cheat filter stays fully active for everything else.

Then `prepare_workspace` runs each `env_setup_commands` entry inside the
container, raising if any exits non-zero.

---

## 7. Stage 4 — the agent and the conversation loop

`run_infer.py:evaluate_instance()` is where a trajectory is actually produced.

**Tools.** `get_default_tools(enable_browser=False)` — bash execution, a file
editor, task tracking, and `finish`. Browsing is disabled: this is a CLI-mode
agent. With `--enable-delegation`, `DelegateTool` is appended so the agent can
spawn sub-agents.

**Agent.**

```python
agent = Agent(llm=self.metadata.llm, tools=tools,
              system_prompt_kwargs={"cli_mode": True})
```

The condenser and security analyzer are deliberately left off (commented in
source). That matters for trajectory interpretation: **there is no context
condensation**, so a long trajectory sends a monotonically growing message list
to the model, and the whole history stays intact in the record.

**Repo staging.** Before the agent sees anything:

1. `repo_path = /workspace/<repo-name>/`
2. Locate the real checkout dynamically:
   `find -L /home -name node_modules -prune -o -name '.git' -type d -print | head -1 | xargs dirname`,
   falling back to `/testbed`.
3. Copy it with `tar -C <src> --ignore-failed-read -cf - . | tar -C <dst> -xf -`
   rather than `cp -r` — `cp -r` aborts on the first unreadable file, and
   Gradle/Java images leave root-owned mode-600 build caches the non-root agent
   user can't read. The pipe's exit status is the *extract* side, so a genuine
   write failure still surfaces.
4. `git reset --hard` so the agent starts from a clean base commit.

**Conversation.**

```python
conversation = Conversation(
    agent=agent, workspace=workspace,
    callbacks=[persist_callback],
    max_iteration_per_run=self.metadata.max_iterations,   # the ceiling
    delete_on_close=True,
    stuck_detection=False,
)
```

`stuck_detection=False` is a long-horizon choice: the SDK's stuck detector would
kill trajectories that legitimately loop while exploring a large repo.

**The instruction.** `get_instruction()` renders a Jinja2 template —
`prompts/long_horizon.j2` by default, `prompts/default.j2` available — with
`instance`, `workspace_dir_name`, `actual_workspace_path`, `metadata`,
`language` (from the instance, falling back to `--lang`), and `use_hint_text`
(from the `USE_HINT_TEXT` env var). If `RUN_WITH_BROWSING` is set, an explicit
"never browse the web" warning is appended. The rendered string is
`conversation.send_message(instruction)` — the first user message of the
trajectory.

**The loop.**

```python
run_conversation_with_fake_user_response(
    conversation,
    run_fn=partial(conversation.run, timeout=metadata.conversation_timeout),
)
```

`conversation.run()` is the think→act→observe loop, bounded by
`max_iteration_per_run` and by a wall-clock `conversation_timeout` per call.
Each iteration:

1. Agent sends the full history + tool schemas to the LLM — **this is the call
   that crosses the bridge.**
2. The response is parsed into either an `ActionEvent` (tool call) or a
   `MessageEvent` (plain text to the user).
3. An action is executed in the container; the result becomes an
   `ObservationEvent`.
4. Every event is appended to `conversation.state.events` and passed to the
   callbacks.
5. Repeat until the agent calls `finish`, the ceiling is reached, or the timeout
   fires.

**The fake user.** A benchmark agent has no human to talk to, so
`benchmarks/utils/fake_user_response.py` intercepts the case where the agent
emits a `MessageEvent` instead of a tool call, and replies:

> "Please continue working on the task on whatever approach you think is
> suitable. When you think you have solved the question, please use the finish
> tool and include your final answer in the message parameter of the finish
> tool. IMPORTANT: YOU SHOULD NEVER ASK FOR HUMAN HELP."

It tracks how many fake responses it has sent and returns `/exit` once the agent
has already delivered an answer. These injected turns are part of the recorded
trajectory — when reading a trajectory, don't mistake them for a real user.

---

## 8. Stage 5 — every LLM call crosses the bridge

From inside the container, litellm issues:

```http
POST http://172.17.0.1:8765/v1/messages
x-api-key: sk-ant-oauth-bridge-stub
anthropic-version: 2023-06-01
content-type: application/json

{"model":"claude-opus-4-8","max_tokens":...,"system":[...],
 "messages":[...],"tools":[...],"stream":true}
```

The bridge's catch-all route handles it (`claude_code_bridge/bridge.py:proxy`):

1. **Read the raw body.** Streaming is detected by a substring probe for
   `"stream":true` — deliberately cheap, avoids reparsing JSON, and a false
   positive is harmless.
2. **System-prefix injection**, only for `POST v1/messages`, only when the body
   parses as a JSON object, and skippable via `AURORA_CC_SKIP_SYSTEM_PREFIX=1`
   (debugging only — real OAuth upstream rejects calls without it). The
   trajectory therefore records the agent's system prompt as the harness built
   it, while Anthropic sees the Claude Code identity string prepended. Bad JSON
   logs a warning and forwards untouched rather than failing the call.
3. **Header rewrite** — strip the inbound auth and hop-by-hop headers, add
   `Authorization: Bearer <token>`, merge `anthropic-beta` (case-insensitively;
   the OAuth beta is *prepended*, caller betas are preserved), default
   `anthropic-version`.
4. **Token acquisition**, off the event loop via `asyncio.to_thread` — it may
   shell out to Keychain, do a synchronous refresh with `time.sleep` backoff, or
   block on an `flock`. On the event loop, one refresh would freeze every
   concurrent worker.
5. **Forward and supervise.** Timeouts are 600s total / 180s per chunk / 30s
   connect. The 180s read timeout is a hard-won number: with extended thinking
   over a large context, a single turn can pause 90–150s between chunks, and
   httpx's 5s default produced `MidStreamFallbackError` storms mid-trajectory.
6. **On 2xx**, forward the upstream status and headers — including `request-id`
   and `anthropic-ratelimit-*` — and stream chunks back verbatim, so the
   trajectory's metrics and your rate-limit visibility stay intact.
7. **On non-2xx**, classify and act (see the table in
   `USAGE_AND_INTEGRATION.md` §13). Retryable throttles are slept and retried
   inline; account problems fail over to the next pool slot or return a
   structured `aurora_bridge` envelope the harness surfaces as an instance
   error.

This is why trajectory runs can survive a rate-limit window: a transient 429 is
absorbed inside the bridge and the agent never sees an interruption. A hard
subscription cap, by contrast, is *not* silently waited out — it becomes a
visible error, because blocking a worker for an hour would corrupt the run's
timing receipts.

Worth stating plainly: **the bridge changes credentials and headers, not
semantics.** Same model, same messages, same tools. Nothing in the trajectory
depends on the bridge being present — a run against a raw `ANTHROPIC_API_KEY`
produces the same *kind* of artifact. The bridge only changes who pays and how
failures are classified.

---

## 9. Stage 6 — harvesting the trajectory

Once `conversation.run()` returns, `evaluate_instance` extracts the deliverable:

```bash
cd <repo_path> && git add -A
git config --global user.email 'evaluation@openhands.dev'
git config --global user.name  'OpenHands Evaluation'
git commit --no-verify -m 'patch'        # --no-verify: husky et al. must not fail the harvest
git --no-pager diff --no-color <base_commit> HEAD
```

The base commit is read from either shape the datasets use —
`instance.data["base"]["sha"]` (SWE-Bench) or `instance.data["base_commit"]`
(Multi-SWE-Bench) — and a missing one is a hard error rather than an empty diff.
The resulting patch text is the **artifact under test**; downstream verification
applies it and runs the instance's tests.

`summarize_instance()` logs a console summary, then the record is assembled:

```python
EvalOutput(
    instance_id=instance.id,
    attempt=self.current_attempt,
    test_result={"git_patch": git_patch, "uuid": instance_uuid},
    instruction=instruction,
    error=None,
    history=list(conversation.state.events),        # ← the trajectory
    metrics=read_live_metrics(workspace, str(conversation.id)),
)
```

Two details:

- **`uuid` is mandatory.** A dataset record without one raises, telling you to
  regenerate the dataset. It's the join key that ties a trajectory to its
  corpus entry across every downstream phase.
- **`read_live_metrics`** (`partial_capture.py`) pulls token/cost stats from the
  agent-server's live stats endpoint rather than reconstructing them from
  events, and round-trips them through `json.dumps(..., allow_nan=False)` so a
  `NaN` cost can never poison the JSONL.

---

## 10. Stage 7 — persistence and the on-disk layout

Trajectories are written in **three** places, for three different reasons.

**(a) Live, per event — the console + structured log stream.**
`benchmarks/utils/conversation.py:build_event_persistence_callback` fires on
every event. It prints a human-readable trajectory line (with a tool-call
index and a shortened instance id), then logs the event as structured JSON.
Events up to **256KB** are logged in full; larger ones log metadata only
(`event_type`, `id`, `timestamp`, `source`, `tool_name`, `tool_call_id`, plus
truncated `command`/`path`/`thought`/`exit_code`) so a single huge observation
can't blow the log pipeline. Persistence is best-effort and wrapped in a
bare `except` — a logging failure must never kill a run.

**(b) Final, per instance — `output.jsonl`.**
`get_default_on_result_writer` appends `out.model_dump_json()` as one line,
under an `fcntl.flock(LOCK_EX)` so parallel workers can't interleave writes.
Records with a non-null `error` go to **`output_errors.jsonl`** instead — so
`output.jsonl` holds only clean trajectories, and the two files together are
the complete set. `generate_error_logs_summary()` renders a human-readable
`ERROR_LOGS.txt` pointing at each failure's logs.

**(c) On failure — `capture.json`.** See [section 12](#12-failure-paths-and-partial-capture).

Layout after a `run_eval.sh` invocation:

```
<out>/<dataset>__<split>/<model>_sdk_<sha>_maxiter_1000[_N_<note>]/
└── run_1/
    ├── output.jsonl                  ← one trajectory per line (clean)
    ├── output_errors.jsonl           ← trajectories that ended in an error
    ├── output.report.json            ← verification report (later phase)
    ├── ERROR_LOGS.txt                ← failure quick-reference
    ├── infer.log                     ← full stdout/stderr of run_infer
    ├── rubric.log                    ← judge phase, if enabled
    ├── metadata.json                 ← config sidecar (incl. the reachable base_url)
    └── logs/
        ├── instance_<id>.log         ← framework messages
        └── instance_<id>.output.log  ← agent conversation output
```

`output.jsonl` is the file to hand to anyone who asks for "the trajectories".

---

## 11. What one trajectory record actually contains

One line of `output.jsonl`, as `EvalOutput`:

| Field | Contents |
|---|---|
| `instance_id` | Task identifier |
| `attempt` | 1-indexed attempt number (iterative mode) |
| `test_result` | `{"git_patch": "<unified diff>", "uuid": "<corpus join key>"}` |
| `instruction` | The fully rendered Jinja2 prompt the agent received |
| `metadata` | The whole `EvalMetadata`, **including the serialised `LLM`** — model, `base_url`, `num_retries`, timeout |
| `history` | `list[Event]` — the trajectory itself |
| `metrics` | `Metrics` — input/output/cache-read/cache-write tokens, cost |
| `error` | `null` on the happy path; the failure string otherwise |
| `instance` | The raw dataset record, optionally |
| `runtime_runs` | Remote-runtime pod/session allocations (remote workspace only) |

The `metadata.llm.base_url` field is what makes a trajectory auditable after the
fact: a record whose `base_url` is `http://172.17.0.1:8765` was generated
through the bridge on a Linux host, and one pointing at `api.anthropic.com`
directly was not.

Reading trajectories back:

```python
import json
from benchmarks.utils.models import EvalOutput

with open("run_1/output.jsonl") as f:
    trajectories = [EvalOutput.model_validate_json(line) for line in f if line.strip()]

t = trajectories[0]
print(t.instance_id, len(t.history), t.metrics.accumulated_cost)
for ev in t.history:
    print(type(ev).__name__, getattr(ev, "tool_name", ""))
```

`EvalOutput` subclasses `OpenHandsModel` specifically so pydantic rebuilds its
schemas when discriminated union event types are registered — otherwise
deserialising a record containing a dynamically registered event type fails.
Import the SDK before validating.

---

## 12. Failure paths and partial capture

`evaluate_instance` wraps the whole post-instruction phase in a `try/except`.
On any exception it calls
`benchmarks/multiswebench/partial_capture.py:capture_interrupted_run` **before**
re-raising, writing a `capture.json` snapshot into the output directory with:

- the partial `git diff` against the base commit (`partial_patch_command`),
- the conversation id and instance metadata,
- live metrics pulled from the agent-server,
- whatever conversation state is recoverable — noting that `RemoteState` and
  `base_state.json` can both be stale while a run is in flight.

The snapshot is written with `json.dump(..., allow_nan=False)`, and the capture
itself is wrapped in its own `try/except`: **diagnostics must never replace the
original inference failure**. A capture failure logs a warning; the real
exception still propagates.

The orchestrator retries the instance up to `--max-retries` (default 3). For
remote workspaces, a runtime crash exponentially raises the resource factor
(`2^runtime_failure_count`, capped at `max_resource_factor`).

Failure classes you'll actually see, and where they come from:

| Symptom in the record | Origin |
|---|---|
| `error` mentioning `aurora_bridge` / `subscription_cap` | Bridge hit the subscription cap and declined to block; refill or configure a pool |
| `error` mentioning `credentials_unavailable` | Bridge couldn't load OAuth credentials — check `./claude_code_bridge.sh check` |
| Connection refused / timeout to the base_url | Bridge not running, wrong platform host, or the egress carve-out didn't apply |
| Empty `git_patch`, no error | The agent genuinely changed nothing — a real (bad) result, not an infrastructure failure |
| `MidStreamFallbackError` | Read timeout too low for extended thinking; leave `AURORA_BRIDGE_READ_TIMEOUT` ≥180 |

That fourth row is the one people misread most often. An empty patch is a
legitimate trajectory.

---

## 13. How the bridge shapes trajectory quality

The bridge is not neutral infrastructure — four of its behaviours show up in
the artifacts:

1. **Absorbed throttles keep trajectories continuous.** A transient 429 or a
   529 is slept and retried inside the bridge, so the agent's history has no gap
   and no spurious error turn. Without it, litellm's own retry would surface as
   agent-visible failure and pollute the reasoning.
2. **Failover preserves long runs.** A 1000-iteration trajectory can outlive one
   account's cap window if `AURORA_CC_ACCOUNT_POOL` has another slot. The switch
   is invisible to the agent.
3. **The system prefix is upstream-only, not in the record.** The harness's
   system prompt is what the trajectory shows; the Claude Code identity string
   is added on the wire. When comparing a bridge trajectory against an
   API-key trajectory, that one-line difference in what the model saw is real
   and worth noting, even though the recorded prompts are identical.
4. **Metrics stay true.** Because upstream headers and the response body pass
   through untouched, token counts and costs in `metrics` are the real upstream
   numbers, not bridge estimates. (Cost figures are computed from litellm's
   price table for the model, so treat them as "as if API-priced" — a
   subscription run has no per-token invoice.)

---

## 14. Traps that silently void a run

These produce artifacts that *look* complete.

**⚠️ The 1000-iteration ceiling.** The hard gate `B4-not-at-turn-ceiling`
compares the recorded `n_episodes` against the iteration ceiling; a run recorded
with a lower ceiling reads as cut off and is **voided**. `run_eval.sh` defaults
`--max-iter` to 1000 — but the older `run_custom_eval.sh` defaults to **300**.
Generating a corpus with the wrong script silently produces trajectories that
every downstream gate rejects. Check the output directory name: it carries
`maxiter_<N>`.

**⚠️ Don't run `make build` on a pinned checkout.** It executes
`git submodule update --init --recursive --remote`, which bumps
`vendor/software-agent-sdk` to upstream tip — contradicting the repo's own
"pin dependencies and submodule versions" principle, and changing `SDK_SHORT_SHA`,
which is baked into both the agent-server image tag and the output path. Safe
equivalent:

```bash
git submodule update --init --recursive        # no --remote
UV_INSECURE_NO_ZIP_VALIDATION=1 uv sync --dev
```

**⚠️ Scorer sources are fingerprinted.** `assay/fingerprint.py` defines
`_SCORER_SOURCES = ("assay/deterministic.py", "assay/compose.py", "assay/report.py")`.
Editing any of them changes `scorer_stamp()` and invalidates every recorded
*score*. Separately, `bundle_fingerprint()` covers rubric items and the
judge-visible TRUTH.md prose sections, so touching a rubric invalidates recorded
*verdicts* and forces a re-judge. This doesn't affect trajectory generation, but
it does affect anything you compute from trajectories afterwards.

**⚠️ `--workspace remote` cannot use the bridge.** The bridge is host-local.
A remote runtime has no route to it. Use `docker`.

**⚠️ One bridge, not one per worker.** `--num-workers 8` means 8 concurrent
agents sharing one bridge — which is correct. Starting a bridge per worker means
N processes racing to refresh the same rotating OAuth grant, and the losers get
invalidated tokens mid-run.

---

## 15. Reproducing a single trajectory by hand

Useful for debugging the bridge path without a full corpus run:

```bash
cd harness

# 1. bridge up and verified from inside a container
proxy/claude_code_bridge.sh start
proxy/claude_code_bridge.sh status
docker run --rm curlimages/curl -s http://172.17.0.1:8765/healthz     # Linux
docker run --rm curlimages/curl -s http://host.docker.internal:8765/healthz  # macOS

# 2. one instance, full ceiling, no SDK retry papering over bridge errors
uv run python -m benchmarks.multiswebench.run_infer \
    proxy/claude-code-oauth.json \
    --dataset data/<bundle>.jsonl --split test --lang python \
    --workspace docker --max-iterations 1000 \
    --conversation-timeout 3600 \
    --num-workers 1 --n-limit 1 --max-attempts 1 \
    --no-llm-retries \
    --output-dir /tmp/one_traj

# 3. inspect
python - <<'PY'
import json
line = open("/tmp/one_traj/.../output.jsonl").readline()
rec = json.loads(line)
print(rec["instance_id"], "events:", len(rec["history"]))
print("patch bytes:", len(rec["test_result"]["git_patch"]))
print("cost:", rec["metrics"]["accumulated_cost"])
PY

# 4. what the bridge saw
tail -50 proxy/logs/claude_code_bridge.log
curl -s localhost:8765/quota | python3 -m json.tool
```

Step 1 is the one to never skip: a bridge that answers on the host but not from
a container is the single most common cause of a run that fails every instance
identically.

---

## Appendix A — component/file map

| Component | Path | Role |
|---|---|---|
| Outer orchestrator | `harness/run_eval.sh` | Dataset staging, images, platform rewrite, pass@k, phases |
| Legacy orchestrator | `harness/run_custom_eval.sh` | Older path — **`--max-iter` defaults to 300** |
| Inference entry | `benchmarks/multiswebench/run_infer.py` | Config load, metadata, workspace, agent, harvest |
| Abstract orchestrator | `benchmarks/utils/evaluation.py` | Worker fan-out, retries, resource escalation |
| Models | `benchmarks/utils/models.py` | `EvalMetadata`, `EvalInstance`, `EvalOutput` |
| Event persistence | `benchmarks/utils/conversation.py` | Per-event console + structured logging |
| Result writer | `benchmarks/utils/evaluation_utils.py` | `output.jsonl` / `output_errors.jsonl`, dir naming |
| Fake user | `benchmarks/utils/fake_user_response.py` | Keeps the agent working when it tries to talk |
| Critics | `benchmarks/utils/critics.py` | Attempt-acceptance policy |
| Partial capture | `benchmarks/multiswebench/partial_capture.py` | `capture.json`, live metrics |
| Prompts | `benchmarks/multiswebench/prompts/{long_horizon,default}.j2` | Instruction templates |
| SDK | `harness/vendor/software-agent-sdk` | OpenHands fork: `Agent`, `Conversation`, tools, workspaces |
| **Bridge** | `harness/proxy/claude_code_bridge/` | OAuth injection, retry, failover |
| **Bridge launcher** | `harness/proxy/claude_code_bridge.sh` | start/stop/status/check/monitor |
| **Bridge LLM config** | `harness/proxy/claude-code-oauth.json` | What `--llm-config` points at |
| Codex bridge | `harness/proxy/codex_bridge/` | OpenAI analogue on :8766 — not in this bundle |

---

## Appendix B — the OAuth paths, and which one is live

`run_infer.py` contains a **second**, independent OAuth mechanism you will see
while reading the source. Don't be misled by it.

If `ANTHROPIC_OAUTH_TOKEN` is set, `run_infer` tries to mount
`harness/proxy/egress-filter-oauth.py` over `/usr/local/bin/egress-filter.py`
inside the container, plus `~/.claude/.credentials.json` at
`/etc/claude_creds.json`, so the in-container mitmproxy addon itself injects the
Bearer token and replaces the dummy `x-api-key` — no host-side proxy involved.

**That file is not present in `harness/proxy/`.** The code handles this: it
checks `os.path.isfile()` and, when the override is missing, logs

```
ANTHROPIC_OAUTH_TOKEN set but .../proxy/egress-filter-oauth.py not found; OAuth injection disabled
```

and proceeds without injection. So setting `ANTHROPIC_OAUTH_TOKEN` in the
current tree does nothing except emit that warning — and the run then fails with
auth errors, because the config's stub key is all LiteLLM has.

**The host-side `claude_code_bridge` is the working OAuth path.** Use it, and
don't set `ANTHROPIC_OAUTH_TOKEN` expecting the other mechanism to engage.
