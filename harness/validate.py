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
        d = src / rel
        if not d.is_dir():
            problems.append(f"missing directory: {rel}/")
        elif rel in ("data", "world_data") and not any(d.rglob("*")):
            # an empty evidence folder builds cleanly and leaves the agent with
            # nothing to read, which looks like a bad model rather than a bad task
            problems.append(f"{rel}/ is empty")

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
        # `mcp_servers` is every server wired into the environment; `apps` is the
        # graded subset. build.py resolves mcp_servers first and falls back to
        # apps, so either one on its own is a complete answer here.
        md = (task.get("metadata") or {})
        if not (md.get("mcp_servers") or md.get("apps")):
            problems.append("task.toml [task.metadata] needs mcp_servers = [...] "
                            "(every server to wire) or apps = [...] (the graded ones)")

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
            # the judge reads these four; a criterion missing one is scored blind
            for i, c in enumerate(criteria, 1):
                if not isinstance(c, dict):
                    problems.append(f"tests/rubric.json criterion {i} is not an object")
                    continue
                for field in ("criterion", "importance", "is_positive"):
                    if field not in c:
                        problems.append(f"tests/rubric.json criterion {i} "
                                        f"({str(c.get('criterion', ''))[:40]}) has no {field}")

    # The grader pulls helpers out of test_outputs at import time. A name it needs
    # and the tests do not define aborts it inside the verifier, which leaves no
    # reward.json and fails the trial after the whole run has been paid for.
    tests_py = src / "tests" / "test_outputs.py"
    grader = src / "tests" / "grade.py"
    if not grader.is_file():
        grader = Path(__file__).resolve().parent.parent / "templates" / "tests" / "grade.py"
    if tests_py.is_file() and grader.is_file():
        body = tests_py.read_text()
        defined = (set(re.findall(r"^(_\w+)\s*(?::[^=]+)?=", body, re.M))      # _CACHE: dict = {}
                   | set(re.findall(r"^def (\w+)", body, re.M))
                   | set(re.findall(r"^(?:import|from)\s+.*\bas\s+(\w+)", body, re.M)))
        # only column-0 binds: an indented one sits behind a hasattr guard
        needed = set(re.findall(r"^(_\w+) = _t\.\1$", grader.read_text(), re.M))
        for name in sorted(needed - defined):
            problems.append(f"tests/grade.py binds {name} at import, which test_outputs.py "
                            f"does not define; the grader would abort and write no reward")

    # the oracle's answers belong in solution/, never where the agent or the judge can see them
    for leaked in ("gt_env.json", "oracle.json"):
        for hit in list(src.glob(f"data/**/{leaked}")) + list(src.glob(f"world_data/**/{leaked}")):
            problems.append(f"{hit.relative_to(src)} would ship to the agent; keep it in solution/")

    if problems:
        raise Failure("input folder does not meet the task contract:\n  - "
                      + "\n  - ".join(problems))
    say(f"input ok: {src.name}")
    return data
