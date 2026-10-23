"""Re-verify a finished job's trials against the task's current verifier.

Used when the agent phase was fine but grading was not - a grader that crashed,
or a fix to the scoring scaffold. `harbor trial regrade` seeds a new trial from
the recorded agent logs and artifacts and runs the verifier over them in its own
container, so the result is produced the same way a live run produces it. The
agent is never re-run and the source trial is never modified.

Note what this does and does not preserve: Channel A is deterministic and comes
back identical, while the rubric is a fresh judgement and will differ from the
original run within the judge's own variance.
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import HARBOR_BIN, JOBS_DIR, VENDOR, Failure, run, say


def _started(trial: Path) -> str:
    try:
        return json.loads((trial / "result.json").read_text()).get("started_at") or ""
    except (OSError, ValueError):
        return ""


def regrade(job: Path, task_dir: Path, judge_env: dict[str, str]) -> Path:
    """Regrade every trial in `job` with the verifier env `judge_env`; return the new job folder."""
    trials = sorted((p for p in job.iterdir() if p.is_dir()), key=_started)
    if not trials:
        raise Failure(f"no trials in {job}")
    dest = JOBS_DIR / f"{job.name}-regraded"
    dest.mkdir(parents=True, exist_ok=True)

    # the trial's config names the bridge agent class; harbor imports it even though
    # the agent never runs again, so it must be importable here as in a live run
    env = {**judge_env, "PYTHONPATH": str(VENDOR / "harness-levi-bridge" / "harbor_agent")}
    say(f"regrading {len(trials)} trials from {job.name} -> {dest.name}")
    for trial in trials:
        name = trial.name
        if (dest / name / "verifier" / "reward.json").is_file():
            say(f"  {name}: already regraded, skipping")
            continue
        run([str(HARBOR_BIN), "trial", "regrade", str(trial),
             "-p", str(task_dir), "-o", str(dest), "--trial-name", name],
            env=env, check=False)
        reward = dest / name / "verifier" / "reward.json"
        if reward.is_file():
            say(f"  {name}: {json.loads(reward.read_text())}")
        else:
            say(f"  {name}: STILL no reward.json; see {dest / name / 'trial.log'}")
    return dest
