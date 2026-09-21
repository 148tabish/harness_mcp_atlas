#!/usr/bin/env python3
"""Package one task and its harbor job into the delivery layout.

    python3 package_task.py --task <task-dir> --job <job-dir> [--out <dir>] [--force]

    python3 package_task.py \\
        --task fixed-tasks/83d7e97e-2aed-4b23-a906-45f5a6a6a4da \\
        --job  fixed-tasks/jobs/oh-wraysbury-p8-low

Writes <out>/<uuid>/ (default out: ./packaged next to this script):


    <uuid>/
      instruction.md        from the task
      task.toml             from the task
      environment/          from the task
      tests/                from the task
      solution/             from the task
      trajectories/
        <model>/            from each trial's lock.json agent.model_name:
                            "anthropic/claude-opus-5" -> "opus-5"
          run_1/ ... run_N/ one per trial of that model, numbered by start time
          pass@N.json       pass count against the task threshold, per-trial rewards,
                            failure-mode histogram (the harness's classify_failure rules)
          pass_summary.json per-run and average test / rubric / combined scores

Each run_i is built from one trial folder:

    run_i/
      agent/trajectory.json     the agent's ATIF trajectory, and nothing else from agent/
      artifacts/
        workspace/              the files the agent produced (/workspace, evidence excluded)
        manifest.json           the trial's manifest, keeping only the /workspace entry
      verifier/                 ctrf.json, reward.json, rubric_breakdown.json,
                                reward_channel_a.json, test-stdout.txt, junit.xml,
                                grade_report.md, and end_env.json when the run has one
      config.json               the trial's config.json, with trials_dir masked and
                                agent.name "openhands_bridge_agent:OpenHandsSDKBridge" ->
                                "openhands-sdk"; everything else kept as recorded
      result.json               the trial's result.json, with: trial_uri masked up to
                                "__<trial id>"; config.trials_dir masked; config.agent.name
                                "openhands_bridge_agent:OpenHandsSDKBridge" -> "openhands-sdk";
                                agent_result.model_usage / rollout_details / metadata and
                                step_results removed. Everything else is kept as recorded.
      report.json               built from the run's own files:
                                  model, run_index
                                  pytest: passed/failed/skipped over the weighted tests
                                    (ctrf.json), exit_code (junit.xml: 1 if any failure or
                                    error), reward = channel_a %, tests[] {name, weight, passed}
                                  rubric[]: rubric_breakdown.json per_criterion, with
                                    id = number and passed = satisfied
                                  final_reward (reward.json), test_weights_percentage
                                  (channel_a), rubric_weights_percentage (rubric score);
                                  percentages truncated to 2 decimals

Logs (artifacts/logs, agent logs, trial.log) and lock.json are left out.
Every trial must belong to the task: a trial whose config.json names a different
task stops the script.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

TASK_ITEMS = ("instruction.md", "task.toml", "environment", "tests", "solution")
VERIFIER_FILES = ("ctrf.json", "reward.json", "rubric_breakdown.json", "reward_channel_a.json",
                  "test-stdout.txt", "junit.xml", "grade_report.md")
VERIFIER_OPTIONAL = ("end_env.json",)   # only when the agent opened a session to snapshot
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def trials_of(job: Path) -> list[Path]:
    """The job's trial folders (those carrying a config.json), ordered by start time."""
    trials = [p for p in job.iterdir() if p.is_dir() and (p / "config.json").is_file()]

    def started(p: Path) -> str:
        try:
            return json.loads((p / "result.json").read_text()).get("started_at") or ""
        except (OSError, ValueError):
            return ""

    return sorted(trials, key=lambda p: (started(p), p.name))


def model_of(trial: Path) -> str:
    """Short model name from lock.json: 'anthropic/claude-opus-5' -> 'opus-5'."""
    lock = json.loads((trial / "lock.json").read_text())
    name = str((lock.get("agent") or {}).get("model_name") or "")
    if not name:
        sys.exit(f"{trial.name}: lock.json has no agent.model_name")
    name = name.rsplit("/", 1)[-1]
    return name[len("claude-"):] if name.startswith("claude-") else name


AGENT_NAMES = {"openhands_bridge_agent:OpenHandsSDKBridge": "openhands-sdk"}


def _mask(text: str) -> str:
    return "*" * len(text)


def _mask_paths(node, roots):
    """Blank out this machine's own directories wherever they appear.

    A build or job path turns up in task_id.path, in config paths and anywhere
    else harbor records where it ran. Walking the whole document rather than
    naming fields keeps a field nobody thought of from leaking one. Only the
    directory is hidden; the task's own folder name (its uuid) stays readable.
    """
    if isinstance(node, dict):
        return {k: _mask_paths(v, roots) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_paths(v, roots) for v in node]
    if isinstance(node, str):
        for root in roots:
            if root in node:
                node = node.replace(root, _mask(root))
        return node
    return node


def local_roots(task_dir, job_dir):
    """The directory prefixes that must never ship: where the task and job live."""
    roots = {str(Path(task_dir).resolve().parent), str(Path(job_dir).resolve().parent),
             str(Path.home())}
    # longest first, so a nested root is masked before its parent
    return tuple(sorted((r for r in roots if r and r != "/"), key=len, reverse=True))


def transform_result(doc: dict) -> dict:
    """result.json for delivery: local paths masked, agent named, empty fields dropped."""
    uri = doc.get("trial_uri")
    if isinstance(uri, str) and "__" in uri:
        head, tail = uri.rsplit("__", 1)
        doc["trial_uri"] = _mask(head) + "__" + tail
    elif isinstance(uri, str):
        doc["trial_uri"] = _mask(uri)
    cfg = doc.get("config") or {}
    if isinstance(cfg.get("trials_dir"), str):
        cfg["trials_dir"] = _mask(cfg["trials_dir"])
    agent = cfg.get("agent") or {}
    if agent.get("name") in AGENT_NAMES:
        agent["name"] = AGENT_NAMES[agent["name"]]
    for key in ("model_usage", "rollout_details", "metadata"):
        (doc.get("agent_result") or {}).pop(key, None)
    doc.pop("step_results", None)
    return doc


def transform_config(doc: dict) -> dict:
    """config.json for delivery: trials_dir masked, agent named."""
    if isinstance(doc.get("trials_dir"), str):
        doc["trials_dir"] = _mask(doc["trials_dir"])
    agent = doc.get("agent") or {}
    if agent.get("name") in AGENT_NAMES:
        agent["name"] = AGENT_NAMES[agent["name"]]
    return doc


def _pct(value) -> float | None:
    """0.225352 -> 22.53: a fraction as a percentage truncated to 2 decimals."""
    if not isinstance(value, (int, float)):
        return None
    return float((Decimal(repr(float(value))) * 100).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _num(value):
    """5.0 -> 5, keeping non-integral numbers as they are."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def build_report(trial: Path, run_index: int) -> dict:
    v = trial / "verifier"
    lock = _load(trial / "lock.json") or {}
    ctrf = (_load(v / "ctrf.json") or {}).get("results") or {}
    grade = _load(v / "reward_channel_a.json") or {}
    reward = _load(v / "reward.json") or {}
    breakdown = _load(v / "rubric_breakdown.json") or {}

    weighted = [t for t in ctrf.get("tests") or [] if "weight" in t]
    exit_code = None
    try:
        root = ET.parse(v / "junit.xml").getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        bad = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
        exit_code = 1 if bad else 0
    except (OSError, ET.ParseError):
        pass

    rubric = []
    for row in breakdown.get("per_criterion") or breakdown.get("results") or []:
        rubric.append({
            "number": row.get("number"),
            "id": row.get("number"),
            "criterion": row.get("criterion"),
            "type": row.get("type"),
            "evaluation_target": row.get("evaluation_target"),
            "importance": row.get("importance"),
            "score": _num(row.get("score")),
            "is_positive": row.get("is_positive"),
            "passed": row.get("satisfied"),
            "satisfied": row.get("satisfied"),
            "justification": row.get("justification"),
        })

    return {
        "model": str((lock.get("agent") or {}).get("model_name") or "").rsplit("/", 1)[-1],
        "run_index": run_index,
        "pytest": {
            "passed": sum(1 for t in weighted if t.get("status") == "passed"),
            "failed": sum(1 for t in weighted if t.get("status") == "failed"),
            "skipped": sum(1 for t in weighted if t.get("status") == "skipped"),
            "exit_code": exit_code,
            "reward": _pct(grade.get("channel_a")),
            "tests": [{"name": t.get("name"), "weight": _num(t.get("weight")),
                       "passed": t.get("status") == "passed"} for t in weighted],
        },
        "rubric": rubric,
        "final_reward": _pct(reward.get("reward")),
        "test_weights_percentage": _pct(grade.get("channel_a")),
        "rubric_weights_percentage": _pct(breakdown.get("score") if breakdown else grade.get("rubric")),
    }


PASS_THRESHOLD_DEFAULT = 0.7       # a run passes at reward >= 0.7 unless test_weights.json sets its own "threshold"


def _mcp_servers(task: Path) -> tuple[str, ...]:
    try:
        import tomllib
    except ImportError:            # python < 3.11: read the names with a pattern
        import re
        return tuple(re.findall(r'^name = "([^"]+)"', (task / "task.toml").read_text(), re.M))
    doc = tomllib.loads((task / "task.toml").read_text())
    return tuple(s.get("name") for s in (doc.get("environment") or {}).get("mcp_servers") or [] if s.get("name"))


def classify_failure(trial: Path, passed: bool, servers: tuple[str, ...]) -> str:
    """The harness's classify_failure, in its order, read off the run's own files."""
    result = _load(trial / "result.json") or {}
    if result.get("exception_info"):
        return "infrastructure"
    if passed:
        return "passed"
    traj = _load(trial / "agent" / "trajectory.json") or {}
    calls = [tc.get("function_name") or "" for s in traj.get("steps") or [] for tc in s.get("tool_calls") or []]
    if not calls:
        return "no_tool_use"
    if not any(c.startswith(tuple(f"{s}_" for s in servers)) for c in calls):
        return "no_mcp_tool_use"
    grade = _load(trial / "verifier" / "reward_channel_a.json") or {}
    if grade.get("guards_tripped"):
        return "guard_violation"
    rows = ((_load(trial / "verifier" / "rubric_breakdown.json") or {}).get("per_criterion")) or []
    if not rows:
        return "grader_incomplete"
    missed = bool(grade.get("missed"))
    rub_miss = any((not r.get("satisfied")) if r.get("is_positive", True) else r.get("satisfied") for r in rows)
    if missed and not rub_miss:
        return "tool_discipline"
    if rub_miss and not missed:
        return "wrong_answer"
    if rub_miss and missed:
        return "partial"
    return "unknown"


def _exact_mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 6) if values else None


def build_pass_files(task: Path, model_dir: Path, model_trials: list[Path]) -> None:
    """pass@N.json and pass_summary.json for one model's runs (already written to run_i/)."""
    weights = _load(task / "tests" / "test_weights.json") or {}
    threshold = weights.get("threshold", PASS_THRESHOLD_DEFAULT)
    servers = _mcp_servers(task)
    per_run, rewards, hist, passed_n = [], {}, {}, 0
    model = None
    for i, trial in enumerate(model_trials, 1):
        rep = _load(model_dir / f"run_{i}" / "report.json") or {}
        model = model or rep.get("model")
        raw = (_load(trial / "verifier" / "reward.json") or {}).get("reward")
        errored = bool((_load(trial / "result.json") or {}).get("exception_info")) or rep.get("test_weights_percentage") is None
        ok = raw is not None and not errored and raw >= threshold
        passed_n += ok
        cls = classify_failure(trial, ok, servers)
        hist[cls] = hist.get(cls, 0) + 1
        rewards[str(i)] = rep.get("final_reward")
        per_run.append({"run_index": i, "test_weights_percentage": rep.get("test_weights_percentage"),
                        "rubric_weights_percentage": rep.get("rubric_weights_percentage"),
                        "combined_score": rep.get("final_reward"), "_errored": errored})
    n = len(model_trials)
    done = [p for p in per_run if not p["_errored"]]
    passk = {
        "model": model, "tasks": 1, "passed": passed_n,
        "accuracy": round(passed_n / n, 6) if n else 0.0,
        "mean_reward_per_trial": _exact_mean([p["combined_score"] for p in done]),
        "mean_pass@k": rewards,
        "failure_mode_histogram": hist,
        "attempts_per_task": n, "at": list(range(1, n + 1)),
    }
    summary = {
        "model": model, "runs": n, "runs_completed": len(done), "runs_errored": n - len(done),
        "errored_run_indices": [p["run_index"] for p in per_run if p["_errored"]],
        "average_combined_score": _exact_mean([p["combined_score"] for p in done]),
        "average_test_weights_percentage": _exact_mean([p["test_weights_percentage"] for p in done]),
        "average_rubric_weights_percentage": _exact_mean([p["rubric_weights_percentage"] for p in done]),
        "per_run": [{k: v for k, v in p.items() if k != "_errored"} for p in per_run],
    }
    (model_dir / f"pass@{n}.json").write_text(json.dumps(passk, indent=2) + "\n")
    (model_dir / "pass_summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def build_run(trial: Path, run: Path, run_index: int, roots: tuple = ()) -> None:
    """Write one run_i folder from a trial folder (layout in the module docstring)."""
    (run / "agent").mkdir(parents=True)
    traj = trial / "agent" / "trajectory.json"
    if traj.is_file():
        shutil.copy2(traj, run / "agent" / "trajectory.json")
    else:
        print(f"  WARNING {trial.name}: no agent/trajectory.json")

    art = run / "artifacts"
    art.mkdir()
    ws = trial / "artifacts" / "workspace"
    if ws.is_dir():
        shutil.copytree(ws, art / "workspace", symlinks=True, ignore=SKIP)
    else:
        (art / "workspace").mkdir()
    try:
        entries = json.loads((trial / "artifacts" / "manifest.json").read_text())
    except (OSError, ValueError):
        entries = []
    kept = [e for e in entries if isinstance(e, dict) and e.get("source") == "/workspace"]
    (art / "manifest.json").write_text(json.dumps(kept, indent=2) + "\n")

    (run / "verifier").mkdir()
    for name in VERIFIER_FILES + VERIFIER_OPTIONAL:
        src = trial / "verifier" / name
        if src.is_file():
            shutil.copy2(src, run / "verifier" / name)
        elif name in VERIFIER_FILES:
            print(f"  WARNING {trial.name}: no verifier/{name}")
    config = _mask_paths(transform_config(json.loads((trial / "config.json").read_text())), roots)
    (run / "config.json").write_text(json.dumps(config, indent=4) + "\n")
    result = _mask_paths(transform_result(json.loads((trial / "result.json").read_text())), roots)
    (run / "result.json").write_text(json.dumps(result, indent=4) + "\n")
    report = build_report(trial, run_index)
    (run / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


def task_of(trial: Path) -> str:
    cfg = json.loads((trial / "config.json").read_text())
    return Path(str((cfg.get("task") or {}).get("path") or "")).name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--task", required=True, type=Path, help="task folder (named by its uuid)")
    ap.add_argument("--job", required=True, type=Path, help="harbor job folder holding the trials")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "packaged",
                    help="where to write <uuid>/ (default: ./packaged next to this script)")
    ap.add_argument("--force", action="store_true", help="replace an existing <out>/<uuid>")
    args = ap.parse_args()

    task, job = args.task.resolve(), args.job.resolve()
    uuid = task.name
    missing = [i for i in TASK_ITEMS if not (task / i).exists()]
    if missing:
        sys.exit(f"task {task} is missing {missing}")
    if not job.is_dir():
        sys.exit(f"no job folder at {job}")

    trials = trials_of(job)
    if not trials:
        sys.exit(f"no trials in {job}")
    wrong = [t.name for t in trials if task_of(t) != uuid]
    if wrong:
        sys.exit(f"trials in {job} belong to another task than {uuid}: {wrong}")

    dest = args.out.resolve() / uuid
    if dest.exists():
        if not args.force:
            sys.exit(f"{dest} already exists (use --force to replace it)")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for item in TASK_ITEMS:
        src = task / item
        if src.is_dir():
            shutil.copytree(src, dest / item, symlinks=True, ignore=SKIP)
        else:
            shutil.copy2(src, dest / item)

    by_model: dict[str, list[Path]] = {}
    for trial in trials:                      # trials are already in start order
        by_model.setdefault(model_of(trial), []).append(trial)

    roots = local_roots(task, job)
    for model, model_trials in by_model.items():
        runs = dest / "trajectories" / model
        runs.mkdir(parents=True)
        for i, trial in enumerate(model_trials, 1):
            build_run(trial, runs / f"run_{i}", i, roots)
            print(f"  trajectories/{model}/run_{i} <- {trial.name}")
        build_pass_files(task, runs, model_trials)
        print(f"  trajectories/{model}/pass@{len(model_trials)}.json, pass_summary.json")

    summary = ", ".join(f"{m} ({len(t)} runs)" for m, t in by_model.items())
    print(f"wrote {dest}: {', '.join(TASK_ITEMS)} and trajectories/ with {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
