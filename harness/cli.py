"""harness cli: validate -> build -> run -> report -> package.

    python -m harness.cli --task input/wraysbury --model opus-5 -k 8 -n 2

Each step can be run on its own (--only build, --only package ...), which is how
a rebuild or a repackage is done without touching the rest.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import build as build_mod
from . import regrade as regrade_mod
from . import report as report_mod
from . import runjob
from .common import (BUILD_DIR, Failure, JOBS_DIR, PACKAGED_DIR, ROOT, die,
                     load_models, open_log, run, say)
from .validate import validate

STEPS = ("validate", "build", "run", "regrade", "report", "package")
# a fresh run grades as it goes; regrade is for re-verifying a job that already ran
DEFAULT_STEPS = tuple(s for s in STEPS if s != "regrade")


def _job_name(src: Path, model_key: str, given: str | None) -> str:
    if given:
        return given
    # seconds, so two runs of one task in the same minute never share a job folder
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{src.name}-{model_key}-{stamp}"


def main(argv: list[str] | None = None) -> int:
    cfg = load_models()
    d = cfg.get("defaults") or {}
    ap = argparse.ArgumentParser(prog="harness", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", required=True, type=Path, help="input folder for the task")
    ap.add_argument("--model", default=str(d.get("model") or "opus-5"),
                    help=f"one of {sorted((cfg.get('models') or {}))}")
    ap.add_argument("--effort", choices=["low", "high", "none"],
                    default=str(d.get("effort", "high")),
                    help="reasoning effort; 'none' leaves the agent's own default")
    ap.add_argument("-k", "--attempts", type=int, default=int(d.get("attempts", 8)))
    ap.add_argument("-n", "--concurrent", type=int, default=int(d.get("concurrent", 2)))
    ap.add_argument("-r", "--retries", type=int, default=int(d.get("retries", 2)))
    ap.add_argument("--job-name")
    # one source for the judge model: the flag, else JUDGE_MODEL from .env, else
    # config/models.yaml. It reaches the verifier as JUDGE_MODEL and is recorded in
    # every run's judge.json; nothing downstream carries a default of its own.
    ap.add_argument("--judge-model",
                    default=os.environ.get("JUDGE_MODEL") or str(d.get("judge_model") or ""),
                    help="model the rubric judge runs as (default: JUDGE_MODEL, else models.yaml)")
    ap.add_argument("--judge-transport", choices=["codex", "claude-code"],
                    default=os.environ.get("JUDGE_TRANSPORT") or str(d.get("judge_transport") or "codex"),
                    help="how Rewardkit reaches the judge model: the codex CLI (ChatGPT login) "
                         "or the claude-code CLI (CLAUDE_CODE_OAUTH_TOKEN)")
    ap.add_argument("--setup-timeout-multiplier", type=float,
                    default=float(d.get("agent_setup_timeout_multiplier", 1.0)),
                    help="scale harbor's 360 s agent-setup budget (the SDK is downloaded per trial; "
                         "raise on a slow link)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="pass@k threshold (default: the task's tests/test_weights.json, else 0.7)")
    ap.add_argument("--only", choices=STEPS, action="append",
                    help="run just these steps (repeatable)")
    ap.add_argument("--no-package", action="store_true", help="stop after the report")
    ap.add_argument("--job", type=Path, help="existing job folder, for --only report/package")
    args = ap.parse_args(argv)

    steps = set(args.only or DEFAULT_STEPS)
    if args.no_package:
        steps.discard("package")
    src = args.task if args.task.is_absolute() else (ROOT / args.task)
    src = src.resolve()
    if not src.is_dir():
        die(f"no such input folder: {src}")

    # one log per run, named after the job it produces, holding both the harness's
    # own lines and everything harbor prints
    job_name = _job_name(src, args.model, args.job_name)
    log_path = open_log(job_name if "run" in steps else f"{src.name}-{'-'.join(sorted(steps))}")
    say(f"logging to {log_path}")

    try:
        # identity first: minting it binds the instruction's canary, so the
        # canary gate in validate always has a record to compare against
        task_uuid, _ = build_mod.identity(src)

        if "validate" in steps:
            validate(src)
        task_dir = BUILD_DIR / task_uuid
        if "build" in steps:
            task_dir = build_mod.build(src, args.judge_transport)
        elif not task_dir.is_dir():
            die(f"{task_dir} is not built yet; run with --only build first")

        job = args.job
        if "run" in steps:
            job = runjob.harbor_run(
                task_dir, args.model, job_name,
                args.attempts, args.concurrent, args.retries,
                None if args.effort == "none" else args.effort,
                args.judge_model, args.judge_transport,
                setup_timeout_multiplier=args.setup_timeout_multiplier)
        elif job is None and steps & {"regrade", "report", "package"}:
            jobs = sorted((p for p in JOBS_DIR.glob("*") if p.is_dir()),
                          key=lambda p: p.stat().st_mtime)
            if not jobs:
                die("no job to work on; pass --job")
            job = jobs[-1]
            say(f"using latest job {job.name}")

        if "regrade" in steps:
            job = regrade_mod.regrade(job, task_dir,
                                      runjob.judge_env(args.judge_model, args.judge_transport))

        if "report" in steps:
            report_mod.summarise(job, task_dir, args.threshold)

        if "package" in steps:
            # every job of this build is packaged, so a second run adds run_N+1 and
            # the pass files are recomputed; the job just run is among them
            PACKAGED_DIR.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, "-m", "harness.package_task", "--task", str(task_dir),
                   "--jobs-root", str(JOBS_DIR), "--out", str(PACKAGED_DIR)]
            if args.threshold is not None:
                cmd += ["--threshold", str(args.threshold)]
            run(cmd, cwd=ROOT)
            say(f"packaged -> {PACKAGED_DIR / task_uuid}")
    except Failure as exc:
        die(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
