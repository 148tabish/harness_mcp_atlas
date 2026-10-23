"""Read a finished job and say what happened, per trial and overall."""
from __future__ import annotations

import json
import statistics
import tomllib
from pathlib import Path

from .common import say, write_json


def _load(trial: Path, rel: str) -> dict:
    try:
        return json.loads((trial / rel).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _mcp_calls(trial: Path, mcp_names: list[str]) -> int:
    traj = _load(trial, "agent/trajectory.json")
    n = 0
    for step in traj.get("steps", []):
        for call in step.get("tool_calls") or []:
            name = call.get("function_name") or ""
            if any(name.startswith(f"{m}_") for m in mcp_names):
                n += 1
    return n


def _mcp_names(task_dir: Path) -> list[str]:
    """The wired server names, parsed as TOML rather than matched as text."""
    try:
        with (task_dir / "task.toml").open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    servers = (doc.get("environment") or {}).get("mcp_servers") or []
    return [s["name"] for s in servers if isinstance(s, dict) and s.get("name")]


def summarise(job: Path, task_dir: Path, threshold: float | None = None) -> dict:
    """threshold None -> the task's own (tests/test_weights.json), else 0.7. Scores are 0-1."""
    from .weights import threshold_of
    if threshold is None:
        threshold = threshold_of(_load(task_dir, "tests/test_weights.json"))
    names = _mcp_names(task_dir)
    trials = []
    for t in sorted(p for p in job.iterdir() if p.is_dir() and (p / "config.json").is_file()):
        reward = _load(t, "verifier/reward.json")
        ledger = _load(t, "verifier/grade_report.json")
        legacy = _load(t, "verifier/reward_channel_a.json")      # pre-Rewardkit verifier
        judge = _load(t, "verifier/judge.json")
        result = _load(t, "result.json")
        exc = ((result.get("exception_info") or {}).get("exception_type")) or ""
        guards = ledger.get("guards_tripped") if ledger else legacy.get("guards_tripped")
        trials.append({
            "trial": t.name.split("__")[-1],
            "reward": reward.get("reward"),
            "pytest": reward.get("pytest", ledger.get("reward") if ledger else legacy.get("channel_a")),
            "rubric": reward.get("rubric"),
            "judge_model": judge.get("model") if judge.get("ran") else None,
            "judge_note": None if judge.get("ran", bool(reward.get("rubric") is not None)) else judge.get("reason"),
            "guards_tripped": len(guards or []),
            "mcp_calls": _mcp_calls(t, names),
            "state_capture_failed": (t / "verifier" / "state_capture_failed.txt").is_file(),
            "exception": exc,
        })
    scored = [x["reward"] for x in trials if isinstance(x["reward"], (int, float))]
    summary = {
        "job": job.name,
        "scale": "0-1",
        "trials": len(trials),
        "scored": len(scored),
        "mean_reward": round(statistics.mean(scored), 6) if scored else None,
        "max_reward": max(scored) if scored else None,
        "passed": sum(1 for x in scored if x >= threshold),
        "threshold": threshold,
        "errored": sum(1 for x in trials if x["exception"]),
        "no_rubric": sum(1 for x in trials if x["rubric"] is None and not x["exception"]),
        "state_capture_failed": sum(1 for x in trials if x["state_capture_failed"]),
        "no_mcp_tool_use": sum(1 for x in trials if x["mcp_calls"] == 0 and not x["exception"]),
        "judge_models": sorted({x["judge_model"] for x in trials if x["judge_model"]}),
        "per_trial": trials,
    }
    write_json(job / "harness_summary.json", summary)

    say(f"{job.name}: {len(trials)} trials, mean reward "
        f"{summary['mean_reward']}, {summary['passed']} at or above {threshold}")
    for x in sorted(trials, key=lambda r: -(r["reward"] or 0)):
        note = f"  <- {x['exception']}" if x["exception"] else ""
        if x["judge_note"]:
            note += f"  (no rubric: {x['judge_note']})"
        if x["state_capture_failed"]:
            note += "  (state capture failed)"
        say(f"   {x['trial']:9} reward {x['reward']} | pytest {x['pytest']} | "
            f"rubric {x['rubric']} | guards {x['guards_tripped']} | mcp {x['mcp_calls']}{note}")
    return summary
