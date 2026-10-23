"""tests/test_weights.json, in both shapes it comes in, and where the rubric lives.

    authoring format   {"tests": {name: weight, ...}, "threshold": 0.97, "null_floor": 0.0,
                        "guard_polarity": "fail_charges"}
    older format       {"components": {"traj_tests": {"weight": w, "tests": {...}}, ...}}

A positive weight earns when the test passes. A negative weight is a guard, charged when
the test fails ("fail_charges", the default: guards are checks that must hold) or, for a
task written to the older template polarity, when it passes ("pass_charges": the guard
detects the misbehaviour). The rubric is tests/judge.toml or the older tests/rubric.json;
harness/judge_compile.py turns either into what the verifier's judge runs.
"""
from __future__ import annotations

from pathlib import Path

GUARD_POLARITIES = ("fail_charges", "pass_charges")


def tests_of(doc: dict) -> dict:
    """name -> weight, whichever shape the document has."""
    comps = doc.get("components") or {}
    return dict(((comps.get("traj_tests") or {}).get("tests") or {}) or doc.get("tests") or {})


def threshold_of(doc: dict, default: float = 0.7) -> float:
    th = doc.get("threshold")
    return float(th) if isinstance(th, (int, float)) and 0 < th <= 1 else default


def guard_polarity(doc: dict) -> str:
    gp = doc.get("guard_polarity", "fail_charges")
    return gp if gp in GUARD_POLARITIES else "fail_charges"


def rubric_file(src: Path) -> Path | None:
    for rel in ("tests/judge.toml", "tests/rubric.json"):
        if (src / rel).is_file():
            return src / rel
    return None
