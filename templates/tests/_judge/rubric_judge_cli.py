#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path


def _cut(value: float, dp: int = 2) -> float:
    """Truncate to `dp` places.

    Decimal on repr(), not int(value * 100) / 100: the float nearest 0.29 is
    0.28999999999999998, which would truncate to 0.28.
    """
    return float(
        Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-dp),
                                             rounding=ROUND_DOWN)
    )

# Optional Headroom prompt compression, default OFF (see grader_compress.py).
# grader_compress.py sits beside this file, which is not necessarily on
# sys.path. A missing or broken module must never stop a trial being graded,
# so the last fallback is an identity function rather than an error.
try:
    from grader_compress import compress_evidence  # type: ignore
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from grader_compress import compress_evidence  # type: ignore
    except ImportError:
        def compress_evidence(model, text):  # type: ignore[misc]
            return text

_out_path: Path | None = None
_token_out: Path | None = None


def _load_criteria(rubric_path: Path) -> list[dict]:
    """Load the rubric, or refuse.

    An unrecognised shape raises rather than returning an empty list: zero
    criteria would grade cleanly and report a finished rubric channel that
    judged nothing.
    """
    raw = json.loads(rubric_path.read_text())
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("criteria"), list):
            return raw["criteria"]
        if isinstance(raw.get("rubric"), list):
            return raw["rubric"]
    raise ValueError(
        f"{rubric_path} must be a list, {{'criteria': [...]}}, or {{'rubric': [...]}}, "
        f"got {type(raw).__name__}"
    )


# Overall evidence ceiling for one judge prompt, in CHARACTERS.
#
# Sized against the worst-case chars/token ratio: these steps are json.dumps
# output and run ~1.8 chars/token, where prose runs ~3.9. 300,000 chars is
# about 167K tokens, leaving margin under a 200K window for the system prompt,
# the rubric and the reply.
_EVIDENCE_BUDGET_CHARS = int(os.environ.get("JUDGE_EVIDENCE_BUDGET_CHARS", "300000"))


def _render_trajectory(traj: dict, budget: int | None = None) -> str:
    """Render a trajectory for the judge, whole steps first, budget last.

    Steps are rendered intact under a single overall budget. When the budget
    binds, whole steps are dropped from the middle rather than truncating one --
    a half-rendered tool call is evidence of nothing -- and the final message is
    always kept complete, since every `evaluation_target: final_answer`
    criterion is graded on it.
    """
    budget = _EVIDENCE_BUDGET_CHARS if budget is None else budget
    steps = traj.get("steps", []) or []

    rendered: list[str] = []
    for step in steps:
        args = step.get("arguments", {})
        resp = step.get("response", "")
        args_s = json.dumps(args) if not isinstance(args, str) else args
        resp_s = json.dumps(resp) if not isinstance(resp, str) else str(resp)
        rendered.append(f"tool={step.get('tool')} args={args_s} → {resp_s}")

    final = traj.get("final_message", "") or ""
    tail = f"Final: {final}" if final else ""

    # The final message is not negotiable; the steps share what is left.
    step_budget = max(0, budget - len(tail))
    total = sum(len(p) + 1 for p in rendered)

    if total > step_budget and rendered:
        # Keep the opening (how the agent framed the task) and the closing (what
        # it actually did), and drop from the middle, which is where repetitive
        # tool churn lives.
        head: list[str] = []
        tail_steps: list[str] = []
        used = 0
        i, j = 0, len(rendered) - 1
        take_head = True
        while i <= j:
            cand = rendered[i] if take_head else rendered[j]
            if used + len(cand) + 1 > step_budget:
                break
            used += len(cand) + 1
            if take_head:
                head.append(cand); i += 1
            else:
                tail_steps.append(cand); j -= 1
            take_head = not take_head
        dropped = len(rendered) - len(head) - len(tail_steps)
        rendered = head + (
            [f"...[{dropped} of {len(steps)} steps omitted: evidence budget "
             f"{budget:,} chars exceeded]"] if dropped else []
        ) + list(reversed(tail_steps))

    parts = rendered + ([tail] if tail else [])
    return "\n".join(parts)


# The models this judge grades with, spelled the way `codex exec -m` wants
# them. The transport is the Codex CLI run as a subprocess, on a `codex login`
# (a ChatGPT subscription); no OPENAI_API_KEY is needed.
CODEX_MODELS = ("gpt-5.6-sol",)

CODEX_EXEC_ARGS = ("exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check")

# The models this judge grades with over the Claude Code CLI, for a verifier
# that has a Claude credential (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in
# [verifier.env]) and the `claude` binary rather than a codex login.
CLAUDE_MODELS = ("claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5")

CLAUDE_EXEC_ARGS = (
    "-p", "--output-format", "json", "--max-turns", "1",
    # A judge reads and returns a verdict. It gets no tools: nothing to grep
    # the trial tree with, nothing to edit, no way to act on what it grades.
    "--allowed-tools", "",
)

# Codex's own agent scaffold, carried on every call on top of the grading
# prompt: a trivial one-line prompt reports about this many input tokens.
CODEX_SCAFFOLD_TOKENS_OBSERVED = 14_564

# Retry policy for the subprocess transport. Transient failures here look
# like nonzero exits or an empty event stream rather than HTTP 429s, but the
# cause is usually the same hot rate-limit window right after an agent run.
_BACKOFF_BASE_SEC = 15.0
_BACKOFF_CAP_SEC = 120.0
_MAX_ATTEMPTS = 4


def _codex_cli() -> str:
    """Path to the Codex CLI, or "" when it is not installed."""
    return shutil.which("codex") or ""


def _claude_cli() -> str:
    """Path to the Claude Code CLI, or "" when it is not installed.

    Checks PATH, then ~/.local/bin, where the installer puts the binary and
    which a non-interactive shell does not add to PATH.
    """
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path(os.path.expanduser("~/.local/bin/claude"))
    return str(candidate) if candidate.is_file() and os.access(str(candidate), os.X_OK) else ""


def _codex_credential_error() -> str:
    """Why the Codex CLI is unusable as a judge, or "" if it is ready.

    Asks the CLI itself (`codex login status`) rather than reading its
    credential store: the answer is what actually matters, and no secret need
    be touched to get it.
    """
    cli = _codex_cli()
    if not cli:
        return (
            "codex CLI not found on PATH; the rubric judge shells out to "
            "`codex exec`, so it must run where the CLI is installed and "
            "logged in (the host, not the task container)"
        )
    try:
        proc = subprocess.run(
            [cli, "login", "status"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as err:
        return f"could not run `codex login status`: {err}"
    out = f"{proc.stdout} {proc.stderr}".strip()
    # A plain substring test for "logged in" would also match "Not logged in",
    # so a nonzero exit or an explicit denial each fail on their own.
    if proc.returncode != 0 or "not logged in" in out.lower():
        return f"codex CLI is not logged in (run `codex login`): {out[:120]}"
    return ""


def _preflight(model: str) -> str | None:
    """Confirm the judge can reach its backend before grading starts.

    For a Codex model the check is the CLI's own `codex login status`. For a
    Claude model it is the binary plus a credential (CLAUDE_CODE_OAUTH_TOKEN
    or ANTHROPIC_API_KEY from [verifier.env]); without one the CLI exits 0
    with a prose auth error instead of a verdict.

    Any other model returns None here and is refused by _run_judge instead.
    """
    if model in CODEX_MODELS:
        return _codex_credential_error() or None
    if model in CLAUDE_MODELS:
        if not _claude_cli():
            return (
                "claude CLI not found on PATH or in ~/.local/bin; the rubric "
                "judge shells out to `claude -p`, so it must run where the CLI "
                "is installed"
            )
        if not (os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")):
            return (
                "neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set; "
                "check task.toml's [verifier.env] passes the token into the "
                "verifier container"
            )
        return None
    return None


def _judge_prompt(criteria: list[dict], traj_ctx: str, final_ctx: str) -> str:
    """The grading prompt. Shared, so the two transports grade identically."""
    criteria_payload = json.dumps(
        [
            {
                "number": c.get("number"),
                "criterion": c.get("criterion"),
                "evaluation_target": c.get("evaluation_target", "trajectory"),
                "is_positive": c.get("is_positive", True),
            }
            for c in criteria
        ],
        indent=2,
    )

    prompt = (
        "You are grading an AI agent's trial output against a rubric.\n\n"
        "## Trajectory\n"
        + traj_ctx
        + "\n\n## Final Message\n"
        + (final_ctx or "(none)")
        + "\n\n## Criteria\n"
        + criteria_payload
        + "\n\nFor each criterion, set satisfied=true when the criterion's statement "
        "is TRUE of the agent's behavior, and false when it is not.\n"
        "Read is_positive carefully. A criterion with is_positive=false states a "
        "mistake, so satisfied=true there means the agent MADE that mistake and "
        "satisfied=false means it avoided it. Judge the statement itself, never "
        "whether the agent did well: on a is_positive=false criterion an agent that "
        "behaved correctly gets satisfied=false.\n"
        "Use trajectory for evaluation_target=trajectory or trajectory_and_state.\n"
        "Use final message for evaluation_target=final_answer.\n"
        "Return JSON: {results: [{number, satisfied (bool), justification (one sentence)}]}"
    )
    return prompt


def _codex_exec(model: str, prompt: str, timeout: float) -> tuple[str, dict]:
    """One `codex exec` call: prompt in on stdin, (reply_text, usage) out.

    Raises JudgeResponseError on a nonzero exit or an empty event stream;
    retry policy belongs to the caller.
    """
    cli = _codex_cli()
    if not cli:
        raise JudgeResponseError("codex CLI not found on PATH")
    proc = subprocess.run(
        [cli, *CODEX_EXEC_ARGS, "-m", model],
        input=prompt, capture_output=True, text=True, timeout=timeout,
        # A scratch cwd, so the read-only agent cannot even see the trial
        # tree it is judging or mistake it for its own workspace.
        cwd=tempfile.gettempdir(),
    )
    if proc.returncode != 0:
        raise JudgeResponseError(
            f"codex exec exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    message, usage_obj = "", {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            # Keep the LAST message: the agent may narrate before it
            # delivers the verdict JSON.
            message = item["text"]
        if event.get("type") == "turn.completed":
            usage_obj = event.get("usage") or {}
    if not message.strip():
        raise JudgeResponseError("codex exec produced no agent message")
    return message, usage_obj


def _claude_exec(model: str, prompt: str, timeout: float) -> tuple[str, dict]:
    """One `claude -p` call: prompt in on stdin, (reply_text, usage) out.

    Same contract as _codex_exec: same return shape, JudgeResponseError on
    transport failure, retry policy left to the caller.

    The CLI answers with a single JSON envelope: {"type": "result", "subtype":
    "success", "is_error": false, "result": "<reply text>", "usage": {...},
    "total_cost_usd": N}. The reply commonly arrives fenced in ```json, which
    _parse_judge_json already unwraps.
    """
    cli = _claude_cli()
    if not cli:
        raise JudgeResponseError("claude CLI not found on PATH or in ~/.local/bin")
    proc = subprocess.run(
        [cli, *CLAUDE_EXEC_ARGS, "--model", model],
        input=prompt, capture_output=True, text=True, timeout=timeout,
        # Same reasoning as the Codex path: a scratch cwd so the judge cannot
        # see the trial tree it is grading or mistake it for its own workspace.
        cwd=tempfile.gettempdir(),
    )
    if proc.returncode != 0:
        raise JudgeResponseError(
            f"claude exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        raise JudgeResponseError(
            f"claude produced no JSON envelope: {proc.stdout.strip()[:300]}"
        ) from err
    # is_error is the CLI's own signal that the turn failed (auth, rate limit,
    # refusal). It still exits 0 in that case, so checking the exit code alone
    # would hand a prose apology to _parse_judge_json and fail one layer later
    # with a much less useful message.
    if envelope.get("is_error"):
        raise JudgeResponseError(
            f"claude reported is_error: {str(envelope.get('result'))[:300]}"
        )
    message = envelope.get("result") or ""
    if not str(message).strip():
        raise JudgeResponseError("claude produced no result text")
    return str(message), (envelope.get("usage") or {}) | {
        "total_cost_usd": envelope.get("total_cost_usd", 0.0)
    }


def _run_judge_claude(
    criteria: list[dict], traj_ctx: str, final_ctx: str, model: str
) -> list[dict]:
    """Grade by running the local Claude Code CLI. Same shape as the Codex path."""
    if not _claude_cli():
        raise JudgeResponseError("claude CLI not found on PATH or in ~/.local/bin")
    prompt = _judge_prompt(criteria, compress_evidence(model, traj_ctx), final_ctx)
    timeout = float(os.environ.get("JUDGE_CLAUDE_TIMEOUT", os.environ.get("JUDGE_CODEX_TIMEOUT", "600")))

    last: Exception | None = None
    text, usage = "", {}
    for n in range(1, _MAX_ATTEMPTS + 1):
        try:
            text, usage = _claude_exec(model, prompt, timeout)
            break
        except (JudgeResponseError, OSError, subprocess.SubprocessError) as err:
            last = err
            if n >= _MAX_ATTEMPTS:
                raise JudgeResponseError(f"claude judge failed: {err}") from err
            delay = min(_BACKOFF_BASE_SEC * 2 ** (n - 1), _BACKOFF_CAP_SEC)
            print(
                f"[judge] claude/{model}: {str(err)[:120]} "
                f"(attempt {n}/{_MAX_ATTEMPTS}), retrying in {delay:.0f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    else:  # pragma: no cover - the loop always breaks or raises
        raise JudgeResponseError(f"claude judge failed: {last}")

    _record_usage_claude(usage, model)

    parsed = _parse_judge_json(text) if text else None
    if isinstance(parsed, dict) and "results" in parsed:
        return parsed["results"]
    if isinstance(parsed, list):
        return parsed
    raise JudgeResponseError(
        "claude judge returned no results array; reply began: "
        + (text[:400] if text else "(empty)")
    )


def _record_usage_claude(usage: dict, model: str) -> None:
    """Usage line for the Claude transport. Best-effort; never raises.

    Anthropic reports input_tokens EXCLUSIVE of the cached counts, unlike
    Codex's inclusive convention -- so there is deliberately no subtraction
    here. Getting that backwards would double-count the cache read.
    """
    if _token_out is None:
        return
    try:
        name = model or os.getenv("JUDGE_MODEL", "")
        fresh = int(usage.get("input_tokens", 0) or 0)
        output = int(usage.get("output_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
        # `claude -p` reports a real total_cost_usd on an API key and 0 on a
        # subscription. Trust the reported figure whenever there is one, and
        # only value the call at list price when the CLI priced it at nothing.
        reported = float(usage.get("total_cost_usd", 0.0) or 0.0)
        line = {
            "model_name": name,
            "judge_input_tokens": fresh,
            "judge_output_tokens": output,
            "judge_input_cache_tokens": cache_read,
            "judge_output_cache_tokens": cache_write,
            "judge_cost_usd": reported or _judge_cost(name, fresh, cache_read, cache_write, output),
        }
        _emit_usage(line)
    except Exception:
        pass


def _run_judge_codex(
    criteria: list[dict], traj_ctx: str, final_ctx: str, model: str
) -> list[dict]:
    """Grade by running the local Codex CLI (`codex exec`).

    Two consequences worth knowing at the call site:

      * Codex is an AGENT, not a completion endpoint. It is pinned to a
        read-only sandbox and run in a scratch cwd so the judge cannot mutate
        the trial it is grading, but it may still take a turn or two to
        answer, so this is slower than an HTTP call.
      * Every call carries Codex's own ~14.5K-token scaffold
        (CODEX_SCAFFOLD_TOKENS_OBSERVED) on top of the grading prompt.
    """
    # A missing CLI is a configuration fact, not a transient failure: refuse
    # before the retry loop rather than backing off three times into it.
    if not _codex_cli():
        raise JudgeResponseError("codex CLI not found on PATH")
    # Only the trajectory goes through compression. The criteria and the
    # surrounding instructions are the verdict contract, and final_ctx is the
    # artifact being judged -- see grader_compress's module docstring for why
    # neither is a legitimate target.
    prompt = _judge_prompt(criteria, compress_evidence(model, traj_ctx), final_ctx)
    timeout = float(os.environ.get("JUDGE_CODEX_TIMEOUT", "600"))

    last: Exception | None = None
    text, usage = "", {}
    for n in range(1, _MAX_ATTEMPTS + 1):
        try:
            text, usage = _codex_exec(model, prompt, timeout)
            break
        except (JudgeResponseError, OSError, subprocess.SubprocessError) as err:
            last = err
            if n >= _MAX_ATTEMPTS:
                raise JudgeResponseError(f"codex judge failed: {err}") from err
            delay = min(_BACKOFF_BASE_SEC * 2 ** (n - 1), _BACKOFF_CAP_SEC)
            print(
                f"[judge] codex/{model}: {str(err)[:120]} "
                f"(attempt {n}/{_MAX_ATTEMPTS}), retrying in {delay:.0f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    else:  # pragma: no cover - the loop always breaks or raises
        raise JudgeResponseError(f"codex judge failed: {last}")

    _record_usage_codex(usage, model)

    parsed = _parse_judge_json(text) if text else None
    if isinstance(parsed, dict) and "results" in parsed:
        return parsed.get("results", [])
    raise JudgeResponseError(
        "judge returned no parseable JSON; reply began: "
        + (text[:400] if text else "(empty)")
    )


_usage_lines: list[dict] = []


def _emit_usage(line: dict) -> None:
    """Append one judge line and rewrite the file with every line so far.

    Lines accumulate in memory (rather than being read back from the file), so
    a re-driven run dir does not stack a previous process's lines on top.
    """
    if _token_out is None:
        return
    _usage_lines.append(line)
    _token_out.parent.mkdir(parents=True, exist_ok=True)
    _token_out.write_text(json.dumps(_usage_lines, indent=2))


def _price(var: str) -> float:
    """A per-million-token rate from the environment; 0.0 when unset or junk."""
    try:
        return float(os.getenv(var, "") or 0.0)
    except ValueError:
        return 0.0


# USD per million tokens, from litellm's model_prices_and_context_window.
# Rates are list price; see _judge_cost for what that does and does not mean.
_JUDGE_RATES: dict[str, dict[str, float]] = {
    "gpt-5.6-sol":       {"input": 5.0, "output": 30.0, "cache_read": 0.5,  "cache_write": 6.25},
    "claude-opus-4-5":   {"input": 5.0, "output": 25.0, "cache_read": 0.5,  "cache_write": 6.25},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3,  "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3,  "cache_write": 3.75},
    "claude-haiku-4-5":  {"input": 1.0, "output": 5.0,  "cache_read": 0.1,  "cache_write": 1.25},
}

_RATE_ENV = {
    "input":       "JUDGE_PRICE_INPUT_PER_MTOK",
    "output":      "JUDGE_PRICE_OUTPUT_PER_MTOK",
    "cache_read":  "JUDGE_PRICE_CACHED_INPUT_PER_MTOK",
    "cache_write": "JUDGE_PRICE_CACHE_WRITE_PER_MTOK",
}


def _rates_for(model: str) -> dict[str, float]:
    """Per-Mtok rates for one judge model: table first, env overrides on top.

    An unknown model prices at zero rather than borrowing another model's rate.
    """
    base = dict(_JUDGE_RATES.get(model, {}))
    for key, var in _RATE_ENV.items():
        if os.getenv(var, "").strip():
            base[key] = _price(var)
    return base


def _judge_cost(model: str, fresh: int, cached: int, cache_write: int, output: int) -> float:
    """Imputed list-price cost of one judge call.

    This is a VALUATION, not an invoice. `codex exec` and `claude -p` spend a
    subscription quota, so no card is charged for these tokens; the number here
    is what the same work would cost at metered list price. Do not add it to a
    provider bill -- it would double count.

    Set any JUDGE_PRICE_*_PER_MTOK to override a rate, or set them all to 0 to
    go back to reporting nothing.
    """
    r = _rates_for(model)
    if not r:
        return 0.0
    return round(
        fresh       / 1e6 * r.get("input", 0.0)
        + output      / 1e6 * r.get("output", 0.0)
        + cached      / 1e6 * r.get("cache_read", 0.0)
        + cache_write / 1e6 * r.get("cache_write", 0.0),
        6,
    )


def _record_usage_codex(usage: dict, model: str) -> None:
    """Token usage line from `codex exec` usage.

    Codex reports OpenAI-style INCLUSIVE input counts: both cached_input_tokens
    and cache_write_input_tokens are subsets of input_tokens. Both are
    subtracted so the buckets stay disjoint and sum back to input_tokens.

    `reasoning_output_tokens` is deliberately not added to judge_output_tokens:
    it is a breakdown of output_tokens, not a sibling of it, so folding it in
    would double count.

    Written best-effort: never raises, never blocks grading.
    """
    if _token_out is None:
        return
    try:
        total_in = int(usage.get("input_tokens", 0) or 0)
        cached = int(usage.get("cached_input_tokens", 0) or 0)
        # Reported by codex as cache_write_input_tokens.
        cache_write = int(usage.get("cache_write_input_tokens", 0) or 0)
        output = int(usage.get("output_tokens", 0) or 0)
        fresh = max(0, total_in - cached - cache_write)
        line = {
            "model_name": model or os.getenv("JUDGE_MODEL", ""),
            "judge_input_tokens": fresh,
            "judge_output_tokens": output,
            "judge_input_cache_tokens": cached,
            # Input-side cache writes.
            "judge_output_cache_tokens": cache_write,
            "judge_cost_usd": _judge_cost(
                model or os.getenv("JUDGE_MODEL", ""), fresh, cached, cache_write, output),
        }
        _emit_usage(line)
    except Exception:
        pass


def _default_judge_model() -> str:
    """Pick a judge model the machine can actually reach.

    An explicit JUDGE_MODEL always wins -- a pinned grader is a benchmark
    property, and this must never silently override one.

    Otherwise the default follows the CLI that is installed: codex first, then
    claude.
    """
    pinned = os.getenv("JUDGE_MODEL", "").strip()
    if pinned:
        return pinned

    # Nothing pinned: the model depends on which CLI is installed, so the
    # choice is announced on stderr where it is made.
    chosen = CODEX_MODELS[0] if _codex_cli() else (
        CLAUDE_MODELS[0] if _claude_cli() else CODEX_MODELS[0])
    print(f"[rubric-judge] WARNING: JUDGE_MODEL is not set; falling back to "
          f"'{chosen}' based on which CLI is installed here. Pin JUDGE_MODEL "
          f"to keep the grader constant across trials.", file=sys.stderr)
    return chosen


async def _run_judge(
    criteria: list[dict], traj_ctx: str, final_ctx: str, model: str
) -> list[dict]:
    """Grade one trial over the local CLI the model id selects.

    A codex model grades through `codex exec`; a Claude model grades through
    `claude -p`. An unusable model is refused with both lists and a note of
    which CLI is missing.
    """
    if model in CODEX_MODELS:
        return _run_judge_codex(criteria, traj_ctx, final_ctx, model)
    if model in CLAUDE_MODELS:
        return _run_judge_claude(criteria, traj_ctx, final_ctx, model)
    raise JudgeResponseError(
        f"{model!r} is not a model this judge grades with. Pass --model, or set "
        f"JUDGE_MODEL, to one of {list(CODEX_MODELS)} (local `codex` CLI, "
        f"available={bool(_codex_cli())}) or {list(CLAUDE_MODELS)} "
        f"(local `claude` CLI, available={bool(_claude_cli())})."
    )


class JudgeResponseError(RuntimeError):
    """The judge ran but its answer could not be read as rubric JSON."""


def _parse_judge_json(text: str):
    """Best-effort JSON out of a model reply: bare, fenced, or embedded in prose."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")      # JSON wrapped in prose
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    return None


def _compute_scores(criteria: list[dict], results: list[dict]) -> dict:
    by_num = {str(r.get("number", "")): r for r in results}
    pos_total = pos_earned = neg_total = neg_hit = 0.0
    per_criterion: list[dict] = []

    for c in criteria:
        num = str(c.get("number", ""))
        score = float(c.get("score", 1))
        is_pos = bool(c.get("is_positive", True))
        r = by_num.get(num, {})
        satisfied = bool(r.get("satisfied", False))

        if is_pos:
            pos_total += score
            if satisfied:
                pos_earned += score
        else:
            neg_total += score
            if satisfied:
                neg_hit += score

        per_criterion.append(
            {
                "number": num,
                "criterion": c.get("criterion"),
                "type": c.get("type"),
                "evaluation_target": c.get("evaluation_target"),
                "importance": c.get("importance"),
                "score": score,
                "is_positive": is_pos,
                "satisfied": satisfied,
                "justification": r.get("justification", ""),
            }
        )

    rc = (pos_earned / pos_total) if pos_total else 1.0
    rb = (neg_hit / neg_total) if neg_total else 0.0
    return {
        # Cut, never rounded, and cut only on the way out: the score is taken
        # from the full-precision rc and rb, so truncating the two inputs does
        # not compound into the number the ledger reads.
        "score": _cut(rc * (1.0 - rb)),
        "rc": _cut(rc),
        "rb": _cut(rb),
        # Pass/fail is decided on the FULL values, not the cut ones. A run that
        # earned 0.999 must not be promoted to a pass by truncation.
        "rubric_passed": rc >= 0.999999 and rb <= 0.000001,
        "per_criterion": per_criterion,
    }


def main() -> None:
    global _out_path, _token_out

    ap = argparse.ArgumentParser()
    ap.add_argument("--rubric", required=True)
    ap.add_argument("--trajectory", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--token-output", default=None,
                    help="write the judge's own token usage here, in the finance "
                         "API's judge_lines shape (read by eval/report/finance/finance_reporter.py)")
    ap.add_argument("--model", default=None,
                    help="judge model; defaults to JUDGE_MODEL, else whichever "
                         "of the codex / claude CLIs is installed here")
    a = ap.parse_args()
    if not a.model:
        a.model = _default_judge_model()

    _out_path = Path(a.output)
    _token_out = Path(a.token_output) if a.token_output else None
    rubric_path = Path(a.rubric)
    traj_path = Path(a.trajectory)

    if not rubric_path.exists():
        print(f"rubric not found: {rubric_path}", file=sys.stderr)
        sys.exit(1)
    if not traj_path.exists():
        print(f"trajectory not found: {traj_path}", file=sys.stderr)
        sys.exit(1)

    # Route to the judge backend before any grading starts. A misconfigured
    # backend that is discovered per-criterion arrives as a rubric full of
    # failures that still writes a score file, which reads like a graded run.
    routing_error = _preflight(a.model)
    if routing_error:
        # A judge that cannot run has not graded anything: exit non-zero and
        # write no breakdown, so the caller leaves the rubric channel unscored
        # instead of reading a zero score.
        print(routing_error, file=sys.stderr)
        sys.exit(4)

    criteria = _load_criteria(rubric_path)
    if not criteria:
        print("no LLM-graded criteria found in rubric", file=sys.stderr)
        _out_path.parent.mkdir(parents=True, exist_ok=True)
        _out_path.write_text(
            json.dumps(
                {"score": 0.0, "rc": 0.0, "rb": 0.0, "rubric_passed": False, "per_criterion": []},
                indent=2,
            )
        )
        sys.exit(0)

    traj = json.loads(traj_path.read_text())
    traj_ctx = _render_trajectory(traj)
    final_ctx = traj.get("final_message", "")

    # Say how much evidence went in. ~1.8 chars/token is the worst-case
    # (JSON-dense) ratio; prose runs nearer 3.9, so this over-estimates.
    _n_steps = len(traj.get("steps") or [])
    _omitted = "...[" in traj_ctx and "steps omitted" in traj_ctx
    print(f"[judge] evidence {len(traj_ctx):,} chars (~{len(traj_ctx)//1.8:,.0f} tok "
          f"worst-case) over {_n_steps} steps, budget {_EVIDENCE_BUDGET_CHARS:,}"
          + ("  ** BUDGET BOUND: steps omitted **" if _omitted else ""))

    print(f"Grading {len(criteria)} criteria with {a.model}")
    results = asyncio.run(_run_judge(criteria, traj_ctx, final_ctx, a.model))

    # A judge that answered but whose reply would not parse must not leave a
    # breakdown of silent falses behind: fail and write nothing, so the trial
    # is recorded as unscored rather than as an agent that satisfied nothing.
    if not results:
        marker = _out_path.parent / "rubric_judge_failed.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"judge produced no parseable verdicts\n"
            f"model: {a.model}\n"
            f"criteria: {len(criteria)}\n"
            f"evidence chars: {len(traj_ctx)}\n"
            f"steps: {_n_steps}\n"
            f"budget bound: {_omitted}\n"
        )
        print(f"ERROR: judge returned no parseable verdicts for {len(criteria)} "
              f"criteria; rubric channel is UNSCORED. See {marker}", file=sys.stderr)
        raise SystemExit(1)

    doc = _compute_scores(criteria, results)

    _out_path.parent.mkdir(parents=True, exist_ok=True)
    _out_path.write_text(json.dumps(doc, indent=2))
    print(f"score={doc['score']} rc={doc['rc']} rb={doc['rb']} passed={doc['rubric_passed']}")
    print(f"Written: {_out_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        traceback.print_exc()
        out = _out_path or Path("/logs/verifier/rubric_breakdown.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"score": 0.0, "rc": 0.0, "rb": 0.0, "rubric_passed": False, "per_criterion": [], "error": str(exc)},
                indent=2,
            )
        )
        sys.exit(1)
