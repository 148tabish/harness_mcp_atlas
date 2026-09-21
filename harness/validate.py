"""Check an input folder against the harness's task contract.

Everything here is a build-time check on what the author wrote. Nothing looks
at a model's output, and nothing here can change a score.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from .common import Failure, say

REQUIRED_FILES = ["instruction.md", "task.toml", "tests/test_outputs.py",
                  "tests/rubric.json", "tests/test_weights.json",
                  "environment/Dockerfile"]
REQUIRED_DIRS = ["data", "world_data", "solution"]

# blocks the harness owns; an author who writes them would be overridden silently
HARNESS_OWNED = ["[agent]", "[verifier]", "[verifier.env]", "[verifier.environment]",
                 "[[verifier.collect]]", "[environment]", "[[environment.mcp_servers]]"]


def validate(src: Path) -> dict:
    """Return the parsed task.toml, or raise Failure with everything that is wrong."""
    problems: list[str] = []

    for rel in REQUIRED_FILES:
        if not (src / rel).is_file():
            problems.append(f"missing file: {rel}")
    for rel in REQUIRED_DIRS:
        if not (src / rel).is_dir():
            problems.append(f"missing directory: {rel}/")

    data: dict = {}
    toml_path = src / "task.toml"
    if toml_path.is_file():
        raw = toml_path.read_text()
        try:
            data = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            problems.append(f"task.toml does not parse: {exc}")
        for block in HARNESS_OWNED:
            if any(line.strip() == block for line in raw.splitlines()):
                problems.append(f"task.toml must not carry {block}; the harness generates it")
        task = data.get("task") or {}
        if not task.get("description"):
            problems.append("task.toml [task] needs a description")
        apps = ((task.get("metadata") or {}).get("apps")) or []
        if not apps:
            problems.append("task.toml [task.metadata] needs apps = [...] (the MCP servers to wire)")

    # tests
    weights_path = src / "tests" / "test_weights.json"
    if weights_path.is_file():
        try:
            weights = json.loads(weights_path.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"tests/test_weights.json does not parse: {exc}")
        else:
            components = weights.get("components") or {}
            traj = (components.get("traj_tests") or {}).get("tests") or {}
            if not traj:
                problems.append("tests/test_weights.json has no components.traj_tests.tests")
            if not (components.get("rubric") or {}).get("file"):
                problems.append("tests/test_weights.json has no components.rubric.file")
            named = set(traj)
            if (src / "tests" / "test_outputs.py").is_file():
                body = (src / "tests" / "test_outputs.py").read_text()
                defined = set(re.findall(r"^def (test_\w+)", body, re.M))
                for miss in sorted(defined - named):
                    problems.append(f"test_outputs.py defines {miss} but it carries no weight")
                for extra in sorted(named - defined):
                    problems.append(f"test_weights.json weights {extra}, which no test defines")
    rubric_path = src / "tests" / "rubric.json"
    if rubric_path.is_file():
        try:
            rubric = json.loads(rubric_path.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"tests/rubric.json does not parse: {exc}")
        else:
            criteria = rubric.get("criteria") or []
            if not criteria:
                problems.append("tests/rubric.json carries no criteria")

    # the oracle's answers belong in solution/, never where the agent or the judge can see them
    for leaked in ("gt_env.json", "oracle.json"):
        for hit in list(src.glob(f"data/**/{leaked}")) + list(src.glob(f"world_data/**/{leaked}")):
            problems.append(f"{hit.relative_to(src)} would ship to the agent; keep it in solution/")

    if problems:
        raise Failure("input folder does not meet the task contract:\n  - "
                      + "\n  - ".join(problems))
    say(f"input ok: {src.name}")
    return data
