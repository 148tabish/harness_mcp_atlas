#!/usr/bin/env python3
"""Package one task and every harbor job that ran it into the delivery layout.

    python -m harness.package_task --task <build-dir> [--jobs <job-dir> ...]
                                   [--jobs-root out/jobs] [--out <dir>] [--threshold 0.97]

Which trials are packaged. Every job under --jobs-root (or named with --jobs) whose
trials ran this task - config.json task.path ends in the uuid - against the task as it
is built now - result.json task_checksum equals the build directory's hash - is taken.
Trials of an older build are left out and named, since their tests/ are not the ones
shipped; --any-checksum takes them anyway. A regraded trial (same trial name, in a job
ending in -regraded) replaces its original. Across all of that, one model's trials are
numbered run_1..run_N by start time, so running the task again adds run_3, run_4 and
pass@N.json / pass_summary.json are recomputed over everything present. The bundle is
derived from out/jobs alone and is rebuilt from scratch on every package.

    <uuid>/
      instruction.md, task.toml, environment/, tests/, solution/   from the built task
      trajectories/
        <model>/              lock.json agent.model_name: "anthropic/claude-opus-5" -> "opus-5";
                              a model with `package_as` in config/models.yaml ships under that
                              name, and `package_model_name` replaces its run-time model name
                              in every file (anthropic/glm-5.3-bedrock -> glm-5.3/, bedrock/glm-5.3)
          run_1/ ... run_N/   one per trial of that model, numbered by start time
          pass@N.json         passes against the threshold, reward per run, failure modes
          pass_summary.json   per-run and average pytest / rubric / combined scores
          jobs.json           the harbor jobs these runs came from, with their harness_summary

Each run_i is built from one trial folder:

    run_i/
      agent/trajectory.json   the agent's ATIF trajectory, and nothing else from agent/
      artifacts/
        workspace/            the files the agent produced (/workspace, evidence excluded)
        manifest.json         the trial's manifest, keeping only the /workspace entry
      verifier/               reward.json {pytest, rubric, reward}, reward-details.json
                              (every test and every criterion with the judge's reasoning),
                              grade_report.json (the ledger), judge.json (what judged, or
                              why nothing did), test-stdout.txt, junit.xml, and the
                              optional files the verifier left (pytest-stdout.txt, ctrf.json,
                              end_env.json, server_tool_calls.log, agent_trajectory.json,
                              final_message.md, rubric_judge_failed.txt, state_capture_failed.txt,
                              reward_producer.json, rewardkit-stdout.txt) and judge_logs/ (the
                              judge's own transcript)
      config.json             the trial's config.json, with trials_dir masked and the agent
                              class named "openhands-sdk"
      result.json             the trial's result.json, with trial_uri and trials_dir masked,
                              agent_result.model_usage / rollout_details / metadata and
                              step_results removed
      report.json             built from the run's own files, every score on the 0-1 scale
                              (6 dp): model, run_index, job, trial, started_at, finished_at,
                              pytest {score, passed, failed, exit_code, guard_polarity,
                              missed, guards_tripped, tests[{name, weight, passed, tripped,
                              reason}]}, rubric[{id, name, criterion, weight, is_positive,
                              satisfied, earned, charged, justification}] or null, rubric_score,
                              judge {ran, transport, model, reason, ...}, final_reward,
                              channels, weights, state_capture_failed, errored

Logs (artifacts/logs, agent logs, trial.log) and lock.json are left out. A trial whose
config.json names a different task stops the script.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from .weights import threshold_of
    from .common import load_models
except ImportError:                      # run as a plain script
    def threshold_of(doc: dict, default: float = 0.7) -> float:
        th = doc.get("threshold")
        return float(th) if isinstance(th, (int, float)) and 0 < th <= 1 else default

    def load_models() -> dict:
        return {}

TASK_ITEMS = ("instruction.md", "task.toml", "environment", "tests", "solution")
VERIFIER_FILES = ("reward.json", "reward-details.json", "grade_report.json", "judge.json",
                  "test-stdout.txt", "junit.xml")
VERIFIER_OPTIONAL = ("pytest-stdout.txt", "ctrf.json", "end_env.json", "server_tool_calls.log",
                     "agent_trajectory.json", "final_message.md", "rubric_judge_failed.txt",
                     "state_capture_failed.txt", "reward_producer.json", "rewardkit-stdout.txt",
                     # written by the pre-Rewardkit verifier; kept so older jobs still package
                     "reward_channel_a.json", "rubric_breakdown.json", "grade_report.md",
                     "judge_tokens.json")
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
AGENT_NAMES = {"openhands_bridge_agent:OpenHandsSDKBridge": "openhands-sdk"}
MASK = "***"
SCALE = "0-1"


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _r6(value):
    return round(float(value), 6) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _num(value):
    """5.0 -> 5, keeping non-integral numbers as they are."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return round(sum(values) / len(values), 6) if values else None


# ------------------------------------------------------------------ which trials
def task_of(trial: Path) -> str:
    cfg = _load(trial / "config.json") or {}
    return Path(str((cfg.get("task") or {}).get("path") or "")).name


def is_trial(p: Path) -> bool:
    return p.is_dir() and (p / "config.json").is_file()


def build_checksum(task: Path) -> str | None:
    """The hash harbor records as result.json task_checksum, for the task as built now."""
    try:
        from dirhash import dirhash
        return dirhash(str(task), "sha256")
    except Exception:            # noqa: BLE001 - no dirhash: the filter is skipped, out loud
        return None


def discover_jobs(root: Path, uuid: str) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(j for j in root.iterdir() if j.is_dir()
                  and any(is_trial(t) and task_of(t) == uuid for t in j.iterdir()))


def _times(trial: Path) -> tuple[str, str]:
    r = _load(trial / "result.json") or {}
    return str(r.get("started_at") or ""), str(r.get("finished_at") or "")


def collect_trials(jobs: list[Path], uuid: str, checksum: str | None,
                   any_checksum: bool = False) -> tuple[list[tuple[Path, str]], list[str]]:
    """[(trial, job name)] in start order, and the notes about what was left out."""
    notes: list[str] = []
    candidates: dict[str, tuple[Path, str]] = {}
    skipped_checksums: dict[str, int] = {}
    for job in jobs:
        for t in sorted(p for p in job.iterdir() if is_trial(p)):
            if task_of(t) != uuid:
                sys.exit(f"{job.name}/{t.name} belongs to another task than {uuid}")
            result = _load(t / "result.json")
            if result is None:
                notes.append(f"{job.name}/{t.name}: no result.json (still running or never finished), left out")
                continue
            tc = result.get("task_checksum")
            if checksum and tc != checksum and not any_checksum:
                skipped_checksums[tc or "?"] = skipped_checksums.get(tc or "?", 0) + 1
                continue
            # an oracle or nop calibration of this build is not a model's trajectory
            agent = (_load(t / "lock.json") or {}).get("agent") or {}
            if not agent.get("model_name"):
                notes.append(f"{job.name}/{t.name}: calibration agent {agent.get('name') or '?'}, left out")
                continue
            name = t.name
            prev = candidates.get(name)
            if prev is None:
                candidates[name] = (t, job.name)
                continue
            # the same trial twice: a regrade replaces its original; otherwise the later one
            regraded_now, regraded_prev = job.name.endswith("-regraded"), prev[1].endswith("-regraded")
            if regraded_now and not regraded_prev or (regraded_now == regraded_prev and _times(t)[1] > _times(prev[0])[1]):
                notes.append(f"{name}: {job.name} replaces {prev[1]}")
                candidates[name] = (t, job.name)
            else:
                notes.append(f"{name}: {prev[1]} replaces {job.name}")
    if skipped_checksums:
        notes.append(f"{sum(skipped_checksums.values())} trial(s) across {len(skipped_checksums)} older "
                     f"build(s) of this task left out (their tests/ are not the ones shipped); "
                     f"--any-checksum packages them too")
    trials = sorted(candidates.values(), key=lambda tj: (_times(tj[0])[0], tj[0].name))
    return trials, notes


# ------------------------------------------------------------------ per trial
def _short(name: str) -> str:
    """'anthropic/claude-opus-5' -> 'opus-5'."""
    name = name.rsplit("/", 1)[-1]
    return name[len("claude-"):] if name.startswith("claude-") else name


def package_map() -> dict[str, str]:
    """How the bundle names a model, from config/models.yaml.

    A model entry may carry `package_as` (the folder under trajectories/) and
    `package_model_name` (what replaces its run-time model name in every file of the
    bundle). The Bedrock GLM route runs as anthropic/glm-5.3-bedrock, which the agent
    needs to reach the bridge, but ships as trajectories/glm-5.3/ with bedrock/glm-5.3
    on every step. Keys are the exact strings that get replaced: the run-time model
    name and its short form; values are their replacements. A model without these
    settings is not in the map and packages under its own name.
    """
    out: dict[str, str] = {}
    try:
        models = (load_models().get("models") or {})
    except Exception:            # noqa: BLE001 - no config here: plain names
        return out
    for key, cfg in models.items():
        if not isinstance(cfg, dict):
            continue
        full = str(cfg.get("model") or "")
        folder = str(cfg.get("package_as") or "")
        bundle_name = str(cfg.get("package_model_name") or "")
        if folder and full:
            out[_short(full)] = folder
            out[str(key)] = folder
        if bundle_name and full:
            out[full] = bundle_name
    return out


_PACKAGE_MAP: dict[str, str] | None = None


def relabel(name: str) -> str:
    """The bundle's name for a run-time model string (unchanged when not mapped)."""
    global _PACKAGE_MAP
    if _PACKAGE_MAP is None:
        _PACKAGE_MAP = package_map()
    return _PACKAGE_MAP.get(name, name)


def provider_of(name: str) -> str:
    return name.split("/", 1)[0] if "/" in name else ""


def relabel_tree(node, model_name: str | None = None):
    """Apply `relabel` to every string value in a JSON tree, exact matches only, so a
    job name or a path that merely contains the model name is left alone. With the
    trial's run-time `model_name`, a mapped model's provider is renamed too (harbor
    records it as a separate "provider" field): anthropic -> bedrock for the GLM route."""
    old_prov = provider_of(model_name) if model_name else ""
    new_prov = provider_of(relabel(model_name)) if model_name else ""
    swap_provider = bool(old_prov and new_prov and old_prov != new_prov)

    def walk(n, key=None):
        if isinstance(n, dict):
            return {k: walk(v, k) for k, v in n.items()}
        if isinstance(n, list):
            return [walk(v) for v in n]
        if isinstance(n, str):
            if swap_provider and key == "provider" and n == old_prov:
                return new_prov
            return relabel(n)
        return n
    return walk(node)


def model_name_of(trial: Path) -> str:
    """The run-time model name from lock.json: 'anthropic/claude-opus-5'."""
    lock = _load(trial / "lock.json") or {}
    name = str((lock.get("agent") or {}).get("model_name") or "")
    if not name:
        sys.exit(f"{trial.name}: lock.json has no agent.model_name")
    return name


def model_of(trial: Path) -> str:
    """Folder name under trajectories/: 'anthropic/claude-opus-5' -> 'opus-5', and a
    mapped model's `package_as` ('anthropic/glm-5.3-bedrock' -> 'glm-5.3')."""
    return relabel(_short(model_name_of(trial)))


def _mask_paths(node, roots):
    """Blank out this machine's own directories wherever they appear."""
    if isinstance(node, dict):
        return {k: _mask_paths(v, roots) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_paths(v, roots) for v in node]
    if isinstance(node, str):
        for root in roots:
            if root in node:
                node = node.replace(root, MASK)
        return node
    return node


def local_roots(task_dir: Path, jobs: list[Path]) -> tuple[str, ...]:
    roots = {str(Path(task_dir).resolve().parent), str(Path.home())}
    roots |= {str(Path(j).resolve().parent) for j in jobs}
    return tuple(sorted((r for r in roots if r and r != "/"), key=len, reverse=True))


def transform_result(doc: dict) -> dict:
    uri = doc.get("trial_uri")
    if isinstance(uri, str) and "__" in uri:
        head, tail = uri.rsplit("__", 1)
        doc["trial_uri"] = MASK + "__" + tail
    elif isinstance(uri, str):
        doc["trial_uri"] = MASK
    cfg = doc.get("config") or {}
    if isinstance(cfg.get("trials_dir"), str):
        cfg["trials_dir"] = MASK
    agent = cfg.get("agent") or {}
    if agent.get("name") in AGENT_NAMES:
        agent["name"] = AGENT_NAMES[agent["name"]]
    for key in ("model_usage", "rollout_details", "metadata"):
        (doc.get("agent_result") or {}).pop(key, None)
    doc.pop("step_results", None)
    return doc


def transform_config(doc: dict) -> dict:
    if isinstance(doc.get("trials_dir"), str):
        doc["trials_dir"] = MASK
    agent = doc.get("agent") or {}
    if agent.get("name") in AGENT_NAMES:
        agent["name"] = AGENT_NAMES[agent["name"]]
    return doc


def _ledger(v: Path) -> dict:
    """The pytest channel in one shape: {score, missed, guards_tripped, errors, polarity, tests[]}.

    reward/pytest/ledger.py writes grade_report.json with tests[{test, weight, passed,
    tripped, reason, error}]; a task-shipped grader wrote the same without `tripped`; the
    pre-Rewardkit verifier wrote reward_channel_a.json (channel_a, missed, guards_tripped).
    """
    rep = _load(v / "grade_report.json") or {}
    rows = [t for t in rep.get("tests") or [] if isinstance(t, dict) and "weight" in t]
    if rows:
        polarity = rep.get("guard_polarity", "fail_charges")
        tests = []
        for t in rows:
            w = t["weight"]
            tripped = t.get("tripped")
            if tripped is None and w < 0:
                tripped = (not t.get("passed")) if polarity == "fail_charges" else bool(t.get("passed"))
            tests.append({"name": t.get("test"), "weight": _num(w), "passed": bool(t.get("passed")),
                          "tripped": bool(tripped) if w < 0 else None,
                          "reason": t.get("reason") or None, "error": t.get("error")})
        return {"score": _r6(rep.get("reward")), "polarity": polarity, "gate": rep.get("gate"),
                "missed": [t["name"] for t in tests if t["weight"] > 0 and not t["passed"]],
                "guards_tripped": [t["name"] for t in tests if t["tripped"]],
                "errors": [t["name"] for t in tests if t["error"]], "tests": tests}
    legacy = _load(v / "reward_channel_a.json") or {}
    if legacy:
        return {"score": _r6(legacy.get("channel_a")), "polarity": "pass_charges", "gate": None,
                "missed": list(legacy.get("missed") or []),
                "guards_tripped": list(legacy.get("guards_tripped") or []), "errors": [], "tests": []}
    return {}


def _rubric_source(task: Path) -> dict[str, dict]:
    """name -> the author's signed weight and target, from the compiled judge.toml's annotations."""
    try:
        import tomllib
        doc = tomllib.loads((task / "tests" / "reward" / "rubric" / "judge.toml").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for c in doc.get("criterion") or []:
        ann = c.get("annotations") or {}
        out[c.get("name")] = {"weight": ann.get("source_weight"), "is_positive": ann.get("is_positive"),
                              "target": ann.get("evaluation_target"), "number": ann.get("number")}
    return out


def _rubric(v: Path, task: Path | None = None) -> tuple[list[dict] | None, float | None]:
    """(criteria rows, channel score) from reward-details.json, else the legacy breakdown.

    A row says what the judge answered (`satisfied`: the criterion text held) and what that
    did to the score: a reward criterion `earned` its weight on yes, a penalty criterion was
    `charged` its weight on yes. The channel is max(0, (earned - charged) / total positive).
    """
    details = _load(v / "reward-details.json") or {}
    entry = details.get("rubric") if isinstance(details, dict) else None
    if isinstance(entry, dict):
        crits = entry.get("criteria")
        if crits is None:
            for comp in entry.get("components") or []:
                crits = (comp.get("detail") or {}).get("criteria")
                if crits:
                    break
        source = _rubric_source(task) if task else {}
        rows = []
        for c in crits or []:
            src = source.get(c.get("name"), {})
            w = c.get("weight", 0.0)
            positive = src.get("is_positive") if src.get("is_positive") is not None else (w >= 0)
            signed = src.get("weight") if src.get("weight") is not None else _num(w)
            raw = c.get("raw")
            held = (str(raw).strip().lower() in ("yes", "true", "1", "1.0")) if raw is not None else None
            rows.append({
                "id": c.get("id") or src.get("number"), "name": c.get("name"),
                "criterion": c.get("description"),
                "weight": signed, "is_positive": positive,
                "evaluation_target": src.get("target"),
                "satisfied": held,
                "earned": abs(signed) if (positive and held) else 0,
                "charged": abs(signed) if (not positive and held) else 0,
                "justification": c.get("reasoning") or None,
                "error": c.get("error"),
            })
        return rows, _r6(entry.get("score"))
    legacy = _load(v / "rubric_breakdown.json") or {}
    rows = []
    for r in legacy.get("per_criterion") or legacy.get("results") or []:
        pos = r.get("is_positive", True)
        rows.append({"id": r.get("number"), "name": r.get("id"), "criterion": r.get("criterion"),
                     "weight": _num(r.get("weight")), "is_positive": pos,
                     "evaluation_target": r.get("evaluation_target"),
                     "satisfied": r.get("satisfied"),
                     "earned": abs(r.get("weight") or 0) if (pos and r.get("satisfied")) else 0,
                     "charged": abs(r.get("weight") or 0) if (not pos and r.get("satisfied")) else 0,
                     "justification": r.get("justification"), "error": None})
    if rows:
        return rows, _r6(legacy.get("score"))
    return None, None


def _judge(v: Path, rubric_rows) -> dict:
    doc = _load(v / "judge.json")
    if isinstance(doc, dict):
        return doc
    failed = v / "rubric_judge_failed.txt"
    if rubric_rows:
        return {"ran": True, "transport": "codex", "model": None, "reason": None,
                "note": "pre-Rewardkit verifier: the judge model was not recorded"}
    return {"ran": False, "transport": None, "model": None,
            "reason": failed.read_text().strip() if failed.is_file() else "no judge output in this trial"}


def build_report(trial: Path, run_index: int, job_name: str, task: Path | None = None) -> dict:
    v = trial / "verifier"
    lock = _load(trial / "lock.json") or {}
    result = _load(trial / "result.json") or {}
    ledger = _ledger(v)
    reward = _load(v / "reward.json") or {}
    rubric_rows, rubric_score = _rubric(v, task)
    judge = _judge(v, rubric_rows)
    if not judge.get("ran"):
        # a judge that did not run (or timed out: every row errored, reward.json has no
        # rubric) scored nothing; reward-details.json still holds its 0.0 rows
        rubric_rows, rubric_score = None, None
    producer = _load(v / "reward_producer.json") or {}
    exit_code = None
    try:
        root = ET.parse(v / "junit.xml").getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        bad = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
        exit_code = 1 if bad else 0
    except (OSError, ET.ParseError):
        pass
    tests = ledger.get("tests") or []
    final = _r6(reward.get("reward"))
    pytest_score = _r6(reward.get("pytest")) if "pytest" in reward else ledger.get("score")
    flag = v / "state_capture_failed.txt"
    return {
        "model": model_of(trial),
        "model_full": relabel(str((lock.get("agent") or {}).get("model_name") or "")),
        "run_index": run_index,
        "job": job_name,
        "trial": trial.name.split("__")[-1],
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "scale": SCALE,
        "pytest": {
            "score": pytest_score,
            "passed": sum(1 for t in tests if t["passed"]),
            "failed": sum(1 for t in tests if not t["passed"]),
            "exit_code": exit_code,
            "guard_polarity": ledger.get("polarity"),
            "gate": ledger.get("gate"),
            "missed": ledger.get("missed", []),
            "guards_tripped": ledger.get("guards_tripped", []),
            "errors": ledger.get("errors", []),
            "tests": tests,
        },
        "rubric": rubric_rows,
        "rubric_score": _r6(reward.get("rubric")) if "rubric" in reward else None,
        "judge": judge,
        "final_reward": final,
        "channels": {k: _r6(val) for k, val in reward.items() if k != "reward"},
        "weights": producer.get("weights"),
        "state_capture_failed": flag.read_text().strip() if flag.is_file() else None,
        "errored": bool(result.get("exception_info")) or final is None,
    }


def _mcp_servers(task: Path) -> tuple[str, ...]:
    import tomllib
    doc = tomllib.loads((task / "task.toml").read_text())
    return tuple(s.get("name") for s in (doc.get("environment") or {}).get("mcp_servers") or [] if s.get("name"))


def classify_failure(trial: Path, report: dict, passed: bool, servers: tuple[str, ...]) -> str:
    """The harness's failure classes, in their order, read off the run's own files."""
    result = _load(trial / "result.json") or {}
    if result.get("exception_info"):
        return "infrastructure"
    if passed:
        return "passed"
    traj = _load(trial / "agent" / "trajectory.json") or {}
    calls = [tc.get("function_name") or "" for s in traj.get("steps") or [] for tc in s.get("tool_calls") or []]
    if not calls:
        return "no_tool_use"
    if servers and not any(c.startswith(tuple(f"{s}_" for s in servers)) for c in calls):
        return "no_mcp_tool_use"
    if report.get("final_reward") is None:
        return "grader_incomplete"
    py = report.get("pytest") or {}
    if py.get("guards_tripped"):
        return "guard_violation"
    missed = bool(py.get("missed"))
    rows = report.get("rubric") or []
    if not rows:
        return "wrong_answer" if missed else "unknown"
    rub_miss = any((r["is_positive"] and not r.get("satisfied")) or (not r["is_positive"] and r.get("satisfied")) for r in rows)
    if missed and not rub_miss:
        return "tool_discipline"
    if rub_miss and not missed:
        return "wrong_answer"
    if rub_miss and missed:
        return "partial"
    return "unknown"


def build_pass_files(task: Path, model_dir: Path, runs: list[tuple[Path, str, dict]],
                     threshold: float) -> None:
    """pass@N.json and pass_summary.json for one model's runs (already written to run_i/)."""
    servers = _mcp_servers(task)
    per_run, rewards, hist, passed_n = [], {}, {}, 0
    for trial, job_name, rep in runs:
        i = rep["run_index"]
        final = rep.get("final_reward")
        errored = bool(rep.get("errored"))
        ok = (not errored) and isinstance(final, (int, float)) and final >= threshold
        passed_n += ok
        cls = classify_failure(trial, rep, ok, servers)
        hist[cls] = hist.get(cls, 0) + 1
        rewards[str(i)] = final
        per_run.append({"run_index": i, "job": job_name, "trial": rep.get("trial"),
                        "pytest_score": (rep.get("pytest") or {}).get("score"),
                        "rubric_score": rep.get("rubric_score"),
                        "combined_score": final, "passed": ok, "errored": errored,
                        "failure_mode": cls,
                        "judge_model": (rep.get("judge") or {}).get("model"),
                        "state_capture_failed": bool(rep.get("state_capture_failed"))})
    n = len(runs)
    done = [p for p in per_run if not p["errored"]]
    model = runs[0][2].get("model") if runs else None
    passk = {
        "model": model, "tasks": 1, "scale": SCALE, "threshold": threshold,
        "passed": passed_n, "pass_rate": round(passed_n / n, 6) if n else 0.0,
        "mean_reward_per_trial": _mean([p["combined_score"] for p in done]),
        "reward_per_run": rewards,
        "failure_mode_histogram": hist,
        "attempts_per_task": n, "at": list(range(1, n + 1)),
        "jobs": sorted({j for _, j, _ in runs}),
    }
    summary = {
        "model": model, "scale": SCALE, "threshold": threshold,
        "runs": n, "runs_completed": len(done), "runs_errored": n - len(done),
        "errored_run_indices": [p["run_index"] for p in per_run if p["errored"]],
        "runs_with_state_capture_failed": [p["run_index"] for p in per_run if p["state_capture_failed"]],
        "judge_models": sorted({p["judge_model"] for p in per_run if p["judge_model"]}),
        "runs_without_rubric": [p["run_index"] for p in per_run if p["rubric_score"] is None],
        "average_combined_score": _mean([p["combined_score"] for p in done]),
        "average_pytest_score": _mean([p["pytest_score"] for p in done]),
        "average_rubric_score": _mean([p["rubric_score"] for p in done]),
        "per_run": per_run,
    }
    (model_dir / f"pass@{n}.json").write_text(json.dumps(passk, indent=2) + "\n")
    (model_dir / "pass_summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def _copy_relabeled(src: Path, dst: Path, run_model: str) -> None:
    """Copy a verifier file; for a mapped model, rewrite the run-time name inside it.

    The judge transcript embeds the whole trajectory as escaped text, so the exact-match
    relabel of JSON values never reaches it. The run-time name is distinctive
    (anthropic/glm-5.3-bedrock), so a plain substring replace is safe. Unmapped
    models are copied byte-identical.
    """
    new = relabel(run_model)
    if new == run_model:
        shutil.copy2(src, dst)
        return
    try:
        text = src.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        shutil.copy2(src, dst)
        return
    dst.write_text(text.replace(run_model, new), encoding="utf-8")
    shutil.copystat(src, dst)


def build_run(trial: Path, job_name: str, run: Path, run_index: int, roots: tuple = (),
              task: Path | None = None) -> dict:
    """Write one run_i folder from a trial folder; return its report."""
    (run / "agent").mkdir(parents=True)
    traj = trial / "agent" / "trajectory.json"
    if traj.is_file():
        if relabel(model_name_of(trial)) == model_name_of(trial):
            shutil.copy2(traj, run / "agent" / "trajectory.json")      # unmapped: byte-identical
        else:
            doc = _load(traj)
            if isinstance(doc, dict):
                for step in doc.get("steps") or []:
                    if isinstance(step, dict) and isinstance(step.get("model_name"), str):
                        step["model_name"] = relabel(step["model_name"])
            (run / "agent" / "trajectory.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    else:
        print(f"  WARNING {trial.name}: no agent/trajectory.json")

    art = run / "artifacts"
    art.mkdir()
    ws = trial / "artifacts" / "workspace"
    if ws.is_dir():
        shutil.copytree(ws, art / "workspace", symlinks=True, ignore=SKIP)
    else:
        (art / "workspace").mkdir()
    entries = _load(trial / "artifacts" / "manifest.json") or []
    kept = [e for e in entries if isinstance(e, dict) and e.get("source") == "/workspace"]
    (art / "manifest.json").write_text(json.dumps(kept, indent=2) + "\n")

    run_model = model_name_of(trial)
    (run / "verifier").mkdir()
    for name in VERIFIER_FILES + VERIFIER_OPTIONAL:
        src = trial / "verifier" / name
        if src.is_file():
            _copy_relabeled(src, run / "verifier" / name, run_model)
        elif name in VERIFIER_FILES:
            print(f"  WARNING {trial.name}: no verifier/{name}")
    # the judge's own transcript and process log (Rewardkit writes them next to reward.json)
    for src in sorted((trial / "verifier").glob("trajectory.*.jsonl")) + sorted((trial / "verifier").glob("process.*.txt")):
        (run / "verifier" / "judge_logs").mkdir(exist_ok=True)
        _copy_relabeled(src, run / "verifier" / "judge_logs" / src.name, run_model)
    config = relabel_tree(_mask_paths(transform_config(_load(trial / "config.json") or {}), roots), run_model)
    (run / "config.json").write_text(json.dumps(config, indent=4) + "\n")
    result = relabel_tree(_mask_paths(transform_result(_load(trial / "result.json") or {}), roots), run_model)
    (run / "result.json").write_text(json.dumps(result, indent=4) + "\n")
    report = build_report(trial, run_index, job_name, task)
    (run / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--task", required=True, type=Path, help="built task folder (named by its uuid)")
    ap.add_argument("--jobs", type=Path, nargs="*", default=[],
                    help="harbor job folders to package (default: every job under --jobs-root that ran this task)")
    ap.add_argument("--jobs-root", type=Path, default=Path(__file__).resolve().parent.parent / "out" / "jobs")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "out" / "packaged",
                    help="where to write <uuid>/")
    ap.add_argument("--threshold", type=float, default=None,
                    help="pass threshold on the 0-1 scale (default: the task's tests/test_weights.json, else 0.7)")
    ap.add_argument("--any-checksum", action="store_true",
                    help="also package trials that ran an older build of this task")
    ap.add_argument("--force", action="store_true", help="accepted for compatibility; the bundle is always rebuilt")
    ap.add_argument("--allow-mixed", action="store_true",
                    help="let runs of two different models share one trajectories/<model> folder "
                         "(models.yaml package_as maps them to the same name)")
    args = ap.parse_args()

    task = args.task.resolve()
    uuid = task.name
    missing = [i for i in TASK_ITEMS if not (task / i).exists()]
    if missing:
        sys.exit(f"task {task} is missing {missing}")
    threshold = args.threshold if args.threshold is not None else threshold_of(_load(task / "tests" / "test_weights.json") or {})
    if not 0 < threshold <= 1:
        sys.exit(f"threshold {threshold} is not in (0, 1]")

    jobs = [j.resolve() for j in args.jobs] or discover_jobs(args.jobs_root.resolve(), uuid)
    for j in jobs:
        if not j.is_dir():
            sys.exit(f"no job folder at {j}")
    if not jobs:
        sys.exit(f"no job under {args.jobs_root} ran task {uuid}")
    checksum = build_checksum(task)
    if checksum is None:
        print("  WARNING: dirhash unavailable; trials of older builds cannot be told apart")
    trials, notes = collect_trials(jobs, uuid, checksum, args.any_checksum)
    for note in notes:
        print(f"  {note}")
    if not trials:
        sys.exit(f"no trial of the current build of {uuid} in {[j.name for j in jobs]}")

    dest = args.out.resolve() / uuid
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for item in TASK_ITEMS:
        src = task / item
        if src.is_dir():
            shutil.copytree(src, dest / item, symlinks=True, ignore=SKIP)
        else:
            shutil.copy2(src, dest / item)

    by_model: dict[str, list[tuple[Path, str]]] = {}
    sources: dict[str, set[str]] = {}
    for trial, job_name in trials:                   # already in start order
        folder = model_of(trial)
        by_model.setdefault(folder, []).append((trial, job_name))
        sources.setdefault(folder, set()).add(model_name_of(trial))
    for folder, names in sources.items():
        if len(names) > 1 and not args.allow_mixed:
            sys.exit(f"trajectories/{folder} would mix runs of {sorted(names)}; package them "
                     f"separately (--jobs) or pass --allow-mixed")

    roots = local_roots(task, jobs)
    for model, model_trials in by_model.items():
        runs_dir = dest / "trajectories" / model
        runs_dir.mkdir(parents=True)
        runs = []
        for i, (trial, job_name) in enumerate(model_trials, 1):
            rep = build_run(trial, job_name, runs_dir / f"run_{i}", i, roots, task)
            runs.append((trial, job_name, rep))
            print(f"  trajectories/{model}/run_{i} <- {job_name}/{trial.name.split('__')[-1]}"
                  f"  reward {rep.get('final_reward')}")
        build_pass_files(task, runs_dir, runs, threshold)
        used = sorted({j for _, j, _ in runs})
        (runs_dir / "jobs.json").write_text(json.dumps([{
            "job": j, "trials": [t.name.split("__")[-1] for t, jn, _ in runs if jn == j],
            "task_checksum": checksum,
            "harness_summary": relabel_tree(_load(next(x for x in jobs if x.name == j) / "harness_summary.json")),
        } for j in used], indent=2) + "\n")
        print(f"  trajectories/{model}/pass@{len(runs)}.json, pass_summary.json, jobs.json")

    summary = ", ".join(f"{m} ({len(t)} runs)" for m, t in by_model.items())
    print(f"wrote {dest}: {', '.join(TASK_ITEMS)} and trajectories/ with {summary} "
          f"from {len(jobs)} job(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
