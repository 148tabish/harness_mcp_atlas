"""Report a job's token and cost usage to the Odoo Finance API, one POST per trial.

    python -m harness.finance --job out/jobs/<job> --task-id <uuid> --model glm-5.3-bedrock --dry-run

Runs as the `finance` step of the cli, after `report`. Everything is switched from
`.env` (see .env.example); with FINANCE_REPORT=false nothing here touches the network.
A trial is posted when all of these hold:

1. FINANCE_REPORT is true (1/true/yes/on).
2. the job's model key is listed in FINANCE_REPORT_MODELS (default glm-5.3-bedrock;
   opus-5 is never listed, so Claude runs never post).
3. ODOO_URL and FINANCE_PROJECT_ID are set.
4. the trial finished with usage: result.json has agent_result and the trajectory has
   final_metrics. An errored or unfinished trial is skipped, never posted as zeros.

Client behaviour, as the Finance API doc asks: 3 attempts with 1 s / 2 s backoff on 5xx,
429 and network errors only; 30 s timeout; a failure is logged and the run goes on
(--strict exits non-zero); a receipt `<trial>/finance_receipt.json` with ok=true blocks a
second post; every post is appended to out/finance_ledger.jsonl.

Token mapping. The OpenHands runner leaves no Claude Code stream, so the doc's fallback
applies: fresh input = total_prompt_tokens - cache reads - cache writes. The runner's
final_metrics are summed from every model response, and this split is what CloudWatch
metered for the Bedrock runs (checked against devops on 2026-10-29). GLM on Bedrock
reports no cache writes, so those fields carry 0. `is_phase_based` is never sent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .common import OUT, ROOT, Failure, load_models, read_env_file, say

ENDPOINT_PATH = "api/v1/ethara_project/trajectory_usage/create"
RETRIES = 3
BACKOFF_S = (1, 2)
TIMEOUT_SEC = 30
TRUE = {"1", "true", "yes", "on"}
RFP_SUB_TYPES = ("Testing", "Sampling")
PRODUCTION_MODES = ("Singlephase", "Multiphase")
DEFAULT_MODELS = "glm-5.3-bedrock"
RECEIPT = "finance_receipt.json"


# ------------------------------------------------------------------ env ---
def _env(name: str, default: str = "") -> str:
    """The process env first (run.sh sources .env), else the repo's .env, else default."""
    val = os.environ.get(name)
    if val is None:
        val = _dotenv().get(name)
    val = (val if val is not None else default).strip()
    return val if val else default


_DOTENV: dict[str, str] | None = None


def _dotenv() -> dict[str, str]:
    global _DOTENV
    if _DOTENV is None:
        path = ROOT / ".env"
        _DOTENV = read_env_file(path) if path.is_file() else {}
    return _DOTENV


def enabled() -> bool:
    return _env("FINANCE_REPORT", "false").lower() in TRUE


def allowed_models() -> set[str]:
    raw = _env("FINANCE_REPORT_MODELS", DEFAULT_MODELS)
    return {m.strip() for m in raw.split(",") if m.strip()}


def gate_models_reason(model_key: str) -> str:
    return f"{model_key} is not in FINANCE_REPORT_MODELS ({', '.join(sorted(allowed_models()))})"


def gate(model_key: str) -> str:
    """"" when this model's trials may post, else the reason they are skipped."""
    if not enabled():
        return "FINANCE_REPORT is off"
    if model_key not in allowed_models():
        return gate_models_reason(model_key)
    if not _env("ODOO_URL"):
        return "ODOO_URL is not set"
    if not _env("FINANCE_PROJECT_ID"):
        return "FINANCE_PROJECT_ID is not set"
    return ""


def budget_fields() -> dict:
    """The documented budget_type rules; a bad combination is a config error."""
    budget = _env("FINANCE_BUDGET_TYPE", "RFP")
    if budget not in ("RFP", "Production"):
        raise ValueError(f"FINANCE_BUDGET_TYPE must be RFP or Production, got {budget!r}")
    if budget == "RFP":
        sub = _env("FINANCE_RFP_SUB_TYPE", "Testing")
        if sub not in RFP_SUB_TYPES:
            raise ValueError(f"FINANCE_RFP_SUB_TYPE must be one of {RFP_SUB_TYPES}, got {sub!r}")
        return {"budget_type": budget, "rfp_sub_type": sub, "production_mode": ""}
    mode = _env("FINANCE_PRODUCTION_MODE")
    if mode not in PRODUCTION_MODES:
        raise ValueError(f"FINANCE_PRODUCTION_MODE must be one of {PRODUCTION_MODES} "
                         f"when FINANCE_BUDGET_TYPE=Production, got {mode!r}")
    return {"budget_type": budget, "rfp_sub_type": "", "production_mode": mode}


def model_name_for(model_key: str) -> str:
    """The name finance sees: package_model_name from models.yaml (bedrock/glm-5.3),
    else the run-time model name, else the key."""
    try:
        entry = (load_models().get("models") or {}).get(model_key) or {}
    except Exception:
        entry = {}
    return str(entry.get("package_model_name") or entry.get("model") or model_key)


# -------------------------------------------------------------- the trial ---
def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _num(value, default=0):
    try:
        if value is None:
            return default
        f = float(value)
        return int(f) if f.is_integer() and not isinstance(default, float) else f
    except (TypeError, ValueError):
        return default


def _iso_utc(raw) -> str:
    """ISO 8601 in UTC with the offset spelled out; now() when the stamp is unusable."""
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def no_usage_reason(trial: Path) -> str:
    """"" when the trial finished with usage, else why it must not be posted."""
    result = _load(trial / "result.json")
    if result is None:
        return "no result.json (the trial has not finished)"
    metrics = (_load(trial / "agent" / "trajectory.json") or {}).get("final_metrics") or {}
    if result.get("agent_result") is None and not metrics:
        exc = (result.get("exception_info") or {}).get("exception_type")
        return "no agent_result and no final_metrics" + (f" (run raised {exc})" if exc else "")
    return ""


def usage_of(trial: Path) -> dict:
    """Tokens, cost, id and time for one trial, split the way the API wants them."""
    result = _load(trial / "result.json") or {}
    metrics = (_load(trial / "agent" / "trajectory.json") or {}).get("final_metrics") or {}
    extra = metrics.get("extra") or {}
    agent_result = result.get("agent_result") or {}

    prompt = _num(metrics.get("total_prompt_tokens", agent_result.get("n_input_tokens")))
    completion = _num(metrics.get("total_completion_tokens", agent_result.get("n_output_tokens")))
    cache_read = _num(extra.get("total_cache_read_input_tokens",
                                metrics.get("total_cached_tokens", agent_result.get("n_cache_tokens"))))
    cache_write = _num(extra.get("total_cache_creation_input_tokens", 0))
    cost = _num(metrics.get("total_cost_usd", agent_result.get("cost_usd")), 0.0)
    fresh = prompt - cache_read - cache_write
    if fresh < 0:                      # cache counted outside the prompt total
        fresh = prompt
    stamp = result.get("finished_at") or result.get("started_at")
    return {
        "trajectory_id": result.get("trial_name") or trial.name,
        "generated_at": _iso_utc(stamp),
        "trajectory_input_tokens": fresh,
        "trajectory_output_tokens": completion,
        "trajectory_input_cache_tokens": cache_read,
        # cache writes, input-side; Odoo stores them under the legacy key today and
        # the correctly named one is sent so it lands once the schema has it
        "trajectory_output_cache_tokens": cache_write,
        "trajectory_cache_write_tokens": cache_write,
        "trajectory_cost_usd": cost,
    }


def judge_lines(trial: Path) -> list[dict]:
    """One line per judge model from verifier/judge.json; [] when the judge did not run."""
    judge = _load(trial / "verifier" / "judge.json") or {}
    if not judge.get("ran"):
        return []
    usage = judge.get("usage") or {}
    models = usage.get("models") or ({judge.get("model") or "": usage} if usage else {})
    out = []
    for name, u in models.items():
        if not isinstance(u, dict):
            continue
        writes = _num(u.get("cache_creation_input_tokens"))
        out.append({
            "model_name": str(name),
            "judge_input_tokens": _num(u.get("input_tokens")),
            "judge_output_tokens": _num(u.get("output_tokens")),
            "judge_input_cache_tokens": _num(u.get("cache_read_input_tokens")),
            "judge_output_cache_tokens": writes,
            "judge_cache_write_tokens": writes,
            "judge_cost_usd": _num(u.get("cost_usd"), 0.0),
            "judge_turns": 0,
        })
    return out


def build_payload(trial: Path, task_id: str, model_key: str) -> dict:
    project_id = _env("FINANCE_PROJECT_ID")
    if not project_id:
        raise ValueError("FINANCE_PROJECT_ID is required (e.g. PRJ-512)")
    usage = usage_of(trial)
    payload = {
        "project_id": project_id,
        "project_type": _env("FINANCE_PROJECT_TYPE", "Technical"),
        "task_id": task_id,
        "trajectory_id": usage["trajectory_id"],
        "team_type": _env("FINANCE_TEAM_TYPE", "Projects"),
    }
    payload.update(budget_fields())
    payload["phase_number"] = _env("FINANCE_PHASE_NUMBER", "1")      # Odoo wants a string
    payload["generated_at"] = usage["generated_at"]
    payload["model_name"] = model_name_for(model_key)
    # Bedrock runs have no Claude org: the value is whatever .env says, "" by default
    payload["subscription_id"] = _env("FINANCE_SUBSCRIPTION_ID", "")
    for key in ("trajectory_input_tokens", "trajectory_output_tokens",
                "trajectory_input_cache_tokens", "trajectory_output_cache_tokens",
                "trajectory_cache_write_tokens", "trajectory_cost_usd"):
        payload[key] = usage[key]
    payload["judge_lines"] = judge_lines(trial)
    return payload


# ------------------------------------------------------------------- http ---
def headers() -> dict:
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    extra = _env("ODOO_EXTRA_HEADERS")
    if extra:
        try:
            hdrs.update({str(k): str(v) for k, v in json.loads(extra).items()})
        except (ValueError, AttributeError) as exc:
            say(f"finance: ODOO_EXTRA_HEADERS is not a JSON object ({exc}); ignored")
    return hdrs


def post(url: str, payload: dict, opener=urllib.request.urlopen, sleep=time.sleep) -> tuple[int, str]:
    """(http status, body). Status 0 means no response after every attempt."""
    body = json.dumps(payload).encode()
    last = ""
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(url, data=body, headers=headers(), method="POST")
        try:
            with opener(req, timeout=TIMEOUT_SEC) as resp:
                return resp.status, resp.read().decode(errors="replace")[:2000]
        except urllib.error.HTTPError as exc:
            text = exc.read().decode(errors="replace")[:2000]
            if exc.code < 500 and exc.code != 429:
                return exc.code, text                   # a client error is final
            last = f"HTTP {exc.code}: {text}"
        except Exception as exc:                        # network, timeout
            last = f"{type(exc).__name__}: {exc}"
        if attempt < RETRIES:
            sleep(BACKOFF_S[min(attempt, len(BACKOFF_S)) - 1])
    return 0, last


# --------------------------------------------------------------- the step ---
def already_reported(trial: Path) -> bool:
    receipt = _load(trial / RECEIPT)
    return bool(isinstance(receipt, dict) and receipt.get("ok"))


def ledger_path() -> Path:
    return Path(_env("FINANCE_LEDGER") or (OUT / "finance_ledger.jsonl"))


def report_trial(trial: Path, task_id: str, model_key: str, *, dry_run: bool = False) -> bool:
    """Post one trial. True when posted (or printed in dry-run), False when skipped or failed."""
    why = no_usage_reason(trial)
    if why:
        say(f"finance: {trial.name}: skipped, {why}")
        return False
    if already_reported(trial):
        say(f"finance: {trial.name}: already reported ({RECEIPT}), skipped")
        return False
    payload = build_payload(trial, task_id, model_key)
    if dry_run:
        print(json.dumps(payload, indent=2))
        return True

    url = f"{_env('ODOO_URL').rstrip('/')}/{ENDPOINT_PATH}"
    status, text = post(url, payload)
    ok = 200 <= status < 300
    say(f"finance: {trial.name} {payload['trajectory_id']} ${payload['trajectory_cost_usd']:.2f} "
        f"{payload['model_name']} -> {'ok' if ok else 'FAILED'} ({status or 'no response'})"
        + ("" if ok else f": {text[:300]}"))
    posted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    receipt = {"trial": trial.name, "endpoint": url, "http_status": status, "ok": ok,
               "response": text, "posted_at": posted_at, "payload": payload}
    try:
        (trial / RECEIPT).write_text(json.dumps(receipt, indent=2) + "\n")
    except OSError as exc:
        say(f"finance: could not write {RECEIPT} ({exc})")
    # the receipt dies with the job dir on a re-run of the same job name; the ledger
    # lives outside it, so what was billed stays auditable
    try:
        ledger = ledger_path()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a") as fh:
            fh.write(json.dumps({"posted_at": posted_at, "ok": ok, "http_status": status,
                                 "task_id": task_id, "trajectory_id": payload["trajectory_id"],
                                 "model_name": payload["model_name"],
                                 "trajectory_cost_usd": payload["trajectory_cost_usd"],
                                 "job": trial.parent.name, "trial": trial.name}) + "\n")
    except OSError as exc:
        say(f"finance: could not append to the ledger ({exc})")
    return ok


def trials_of(job: Path) -> list[Path]:
    return sorted(p for p in job.iterdir() if p.is_dir() and (p / "config.json").is_file())


def report_job(job: Path, task_id: str, model_key: str, *, dry_run: bool = False,
               strict: bool = False) -> dict:
    """Post every finished trial of a job that the gate lets through.

    Returns {"posted": n, "skipped": n, "failed": n}. Never raises on a failed post
    unless strict; a configuration error (bad budget fields) is reported the same way.
    """
    counts = {"posted": 0, "skipped": 0, "failed": 0}
    if dry_run:                 # a preview needs neither the switch nor the URL
        why = "" if model_key in allowed_models() else gate_models_reason(model_key)
    else:
        why = gate(model_key)
    if why:
        say(f"finance: off for {job.name} ({why})")
        if strict and why != "FINANCE_REPORT is off":
            raise Failure(f"finance: {why}")
        return counts
    try:
        budget_fields()
    except ValueError as exc:
        say(f"finance: cannot build payloads: {exc}")
        if strict:
            raise Failure(f"finance: {exc}") from exc
        counts["failed"] = len(trials_of(job))
        return counts
    for trial in trials_of(job):
        try:
            if report_trial(trial, task_id, model_key, dry_run=dry_run):
                counts["posted"] += 1
            elif already_reported(trial) or no_usage_reason(trial):
                counts["skipped"] += 1
            else:
                counts["failed"] += 1
        except Exception as exc:                        # one trial never blocks the rest
            say(f"finance: {trial.name}: {type(exc).__name__}: {exc}")
            counts["failed"] += 1
    say(f"finance: {job.name}: {counts['posted']} posted, {counts['skipped']} skipped, "
        f"{counts['failed']} failed" + (" (dry run)" if dry_run else ""))
    if strict and counts["failed"]:
        raise Failure(f"finance: {counts['failed']} trial(s) not reported")
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness.finance", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", required=True, type=Path, help="out/jobs/<job>")
    ap.add_argument("--task-id", required=True, help="the task uuid")
    ap.add_argument("--model", required=True, help="the model key the job ran with")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the payloads, send nothing (works with FINANCE_REPORT=false)")
    ap.add_argument("--strict", action="store_true", help="exit non-zero when a post fails")
    args = ap.parse_args(argv)
    job = args.job if args.job.is_absolute() else (ROOT / args.job)
    if not job.is_dir():
        print(f"no such job: {job}", file=sys.stderr)
        return 2
    try:
        report_job(job, args.task_id, args.model, dry_run=args.dry_run, strict=args.strict)
    except Failure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
