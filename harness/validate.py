"""Check an input folder against the harness's task contract.

Everything here is a build-time check on what the author wrote. Nothing looks
at a model's output, and nothing here can change a score.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from . import gates
from .common import BUILD_DIR, Failure, say

REQUIRED_FILES = ["instruction.md", "task.toml", "tests/test_outputs.py",
                  "tests/rubric.json", "tests/test_weights.json",
                  "tests/oracle.json", "environment/Dockerfile",
                  # the oracle, the world it produces and the answer it states:
                  # a solution/ that only exists as a directory cannot be replayed
                  "solution/solve.py", "solution/gt_env.json", "solution/TRUTH.md"]
REQUIRED_DIRS = ["data", "world_data", "solution"]

# a world overlay with one placeholder file is an empty world with extra steps
MIN_WORLD_FILES = 2

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
        elif rel in ("data", "world_data"):
            # an empty evidence folder builds cleanly and leaves the agent with
            # nothing to read, which looks like a bad model rather than a bad task
            files = [p for p in d.rglob("*") if p.is_file()]
            if not files:
                problems.append(f"{rel}/ is empty")
            elif rel == "world_data" and len(files) < MIN_WORLD_FILES:
                problems.append(f"world_data/ holds {len(files)} file; an overlay that seeds "
                                f"nothing leaves the apps at their stock corpus")

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
        wired, graded = md.get("mcp_servers") or [], md.get("apps") or []
        if not (wired or graded):
            problems.append("task.toml [task.metadata] needs mcp_servers = [...] "
                            "(every server to wire) or apps = [...] (the graded ones)")
        # build.py wires mcp_servers when both are given, so a graded app missing
        # from that list would never be wired and every check on it would fail
        for app in sorted(set(graded) - set(wired)) if (wired and graded) else []:
            problems.append(f"apps lists {app}, which mcp_servers does not wire")
        if not data.get("schema_version"):
            problems.append('task.toml has no schema_version (the contract version '
                            'its blocks are written against, e.g. "1.4")')

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
            # the reward a run that does nothing must not clear: without it stated,
            # nobody can say what score an empty submission would have earned
            if weights.get("null_floor") is None:
                problems.append("tests/test_weights.json has no null_floor: the reward an "
                                "empty submission scores, which a real run must beat")
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

    # A floating tag rebuilds into a different image months later, so a run can
    # never be reproduced and two runs cannot be compared.
    dockerfile = src / "environment" / "Dockerfile"
    if dockerfile.is_file():
        for line in dockerfile.read_text().splitlines():
            if line.strip().upper().startswith("FROM ") and "@sha256:" not in line:
                problems.append(f"environment/Dockerfile is not pinned to a digest: "
                                f"{line.strip()[:60]}")

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

    # the answer must sit only where the grader reads it, the instruction's canary
    # must be this task's own, and the solution's wording must not already be on
    # the agent's side of the boundary
    problems += gates.private_boundary(src)
    problems += gates.canary_binding(src)
    problems += gates.containment(src)

    # what the bundle claims about where it came from and what it was screened against
    problems += gates.provenance(data)
    problems += gates.contamination_screen(src, data)
    problems += gates.collision(src, BUILD_DIR.parent / "task_registry.json")

    if problems:
        raise Failure("input folder does not meet the task contract:\n  - "
                      + "\n  - ".join(problems))
    say(f"input ok: {src.name}")
    return data
