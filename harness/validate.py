"""Check an input folder against the harness's task contract.

Everything here is a build-time check on what the author wrote. Nothing looks
at a model's output, and nothing here can change a score.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

from . import gates, server_patches
from .common import BUILD_DIR, Failure, say
from .judge_compile import compile_judge
from .weights import GUARD_POLARITIES, rubric_file, tests_of

REQUIRED_FILES = ["instruction.md", "task.toml", "tests/test_outputs.py",
                  "tests/test_weights.json",
                  "tests/oracle.json", "environment/Dockerfile",
                  # the oracle, the world it produces and the answer it states:
                  # a solution/ that only exists as a directory cannot be replayed
                  "solution/solve.py", "solution/gt_env.json", "solution/TRUTH.md"]
REQUIRED_DIRS = ["data", "world_data", "solution"]

# a world overlay with one placeholder file is an empty world with extra steps
MIN_WORLD_FILES = 2

# The rubric: tests/judge.toml ([[criterion]] tables, the authoring format) or the
# older tests/rubric.json ({"criteria": [...]}). Either is compiled for the judge at build.

# Provenance and contamination records are CRUCIBLE policy, not task format. They
# refuse the build only when ATLAS_STRICT_PROVENANCE=1; otherwise they are said
# out loud and the task builds, so a task can be run before its paperwork lands.
STRICT_PROVENANCE = os.environ.get("ATLAS_STRICT_PROVENANCE", "") == "1"

# blocks the harness owns; an author who writes them would be overridden silently
HARNESS_OWNED = ["[agent]", "[verifier]", "[verifier.env]", "[verifier.environment]",
                 "[[verifier.collect]]", "[environment]", "[[environment.mcp_servers]]"]


def _copy_case_mismatches(context: Path) -> list[str]:
    """COPY/ADD sources in context/Dockerfile that exist only up to letter case."""
    import fnmatch
    import shlex
    dockerfile = context / "Dockerfile"
    if not dockerfile.is_file():
        return []
    out: list[str] = []
    text = dockerfile.read_text().replace("\\\n", " ")
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or parts[0].upper() not in ("COPY", "ADD"):
            continue
        rest = parts[1]
        if rest.lstrip().startswith("["):                     # JSON form
            try:
                import json
                args = json.loads(rest)
            except ValueError:
                continue
        else:
            args = shlex.split(rest)
        if any(a.startswith("--from=") for a in args):      # another build stage
            continue
        sources = [a for a in args if not a.startswith("--")][:-1]
        for pattern in sources:
            if pattern.startswith(("http://", "https://")):
                continue
            exact = _exists_exact(context, pattern, fnmatch)
            if exact is False:
                out.append(f"environment/Dockerfile copies {pattern!r}, which exists only in a "
                           f"different letter case (fine on a Mac, a build error on Linux)")
    return out


def _exists_exact(root: Path, pattern: str, fnmatch) -> bool | None:
    """True if `pattern` (a path, maybe with globs) matches with exact case, False if it
    matches only case-insensitively, None if nothing matches either way."""
    parts = [p for p in pattern.strip("/").split("/") if p and p != "."]
    exact_hits, loose_hits = [root], [root]
    for part in parts:
        nxt_exact, nxt_loose = [], []
        for d in exact_hits:
            if d.is_dir():
                nxt_exact += [d / n for n in sorted(e.name for e in d.iterdir()) if fnmatch.fnmatchcase(n, part)]
        for d in loose_hits:
            if d.is_dir():
                nxt_loose += [d / n for n in sorted(e.name for e in d.iterdir()) if fnmatch.fnmatchcase(n.lower(), part.lower())]
        exact_hits, loose_hits = nxt_exact, nxt_loose
        if not loose_hits:
            return None
    if exact_hits:
        return True
    return False if loose_hits else None


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
    if not rubric_file(src):
        problems.append("missing file: tests/judge.toml (or the older tests/rubric.json)")
    elif (src / "tests" / "judge.toml").is_file() and (src / "tests" / "rubric.json").is_file():
        # the build compiles judge.toml and the older verifier used rubric.json, so a task
        # shipping both can be judged against whichever is the leftover of another task
        # (8522713f: a foreign judge.toml beside its own rubric.json scored its oracle 0.0)
        problems.append("tests/judge.toml and tests/rubric.json both present: keep the one that "
                        "is this task's rubric (the build compiles judge.toml; the task's own "
                        "verifier used rubric.json)")
    weights_path = src / "tests" / "test_weights.json"
    if weights_path.is_file():
        try:
            weights = json.loads(weights_path.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"tests/test_weights.json does not parse: {exc}")
        else:
            # flat {"tests": {name: weight}} (the authoring format) or the older
            # {"components": {"traj_tests": {"tests": {...}}}}; weights_of reads both
            traj = tests_of(weights)
            if not traj:
                problems.append('tests/test_weights.json has no "tests" map '
                                "(nor components.traj_tests.tests)")
            bad = {n: w for n, w in traj.items() if not isinstance(w, (int, float)) or w == 0}
            for name, w in sorted(bad.items()):
                problems.append(f"tests/test_weights.json gives {name} the weight {w!r}; "
                                f"weights are non-zero numbers (negative = guard)")
            # the reward a run that does nothing must not clear: without it stated,
            # nobody can say what score an empty submission would have earned
            if weights.get("null_floor") is None:
                problems.append("tests/test_weights.json has no null_floor: the reward an "
                                "empty submission scores, which a real run must beat")
            th = weights.get("threshold")
            if th is not None and not (isinstance(th, (int, float)) and 0 < th <= 1):
                problems.append(f"tests/test_weights.json threshold {th!r} is not in (0, 1]")
            gp = weights.get("guard_polarity", "fail_charges")
            if gp not in GUARD_POLARITIES:
                problems.append(f"tests/test_weights.json guard_polarity {gp!r} is not one of "
                                f"{GUARD_POLARITIES}")
            # Two guard conventions exist: a guard that asserts the rule held (charged when
            # it fails) and one that asserts the misbehaviour happened (charged when it
            # passes). Read the wrong way, every clean run is charged every guard.
            if any(w < 0 for w in traj.values() if isinstance(w, (int, float))) \
                    and "guard_polarity" not in weights:
                say('WARNING tests/test_weights.json has guards but no "guard_polarity"; '
                    'assuming "fail_charges" (a guard is charged when it fails). A guard that '
                    'passes when the misbehaviour is detected needs "pass_charges".')
            named = set(traj)
            if (src / "tests" / "test_outputs.py").is_file():
                body = (src / "tests" / "test_outputs.py").read_text()
                # pytest collects module-level callables by attribute name, so a test
                # bound by assignment (test_sheet_gpu_a100 = _sheet_test("gpu-a100"),
                # eeafd50a) counts as defined too
                defined = set(re.findall(r"^def (test_\w+)", body, re.M)) \
                    | set(re.findall(r"^(test_\w+)\s*=", body, re.M))
                for miss in sorted(defined - named):
                    problems.append(f"test_outputs.py defines {miss} but it carries no weight")
                # a module that mints tests at import time (globals()["test_%s" % k] = _mk(k),
                # c5ff46fc) cannot be enumerated without running it; the oracle calibration is
                # the check that every weighted name is collected, so here it is a warning
                dynamic = re.search(r'globals\(\)\s*\[\s*["\']test_', body) is not None
                for extra in sorted(named - defined):
                    msg = f"test_weights.json weights {extra}, which no test defines"
                    if dynamic:
                        say(f"WARNING {msg} statically (tests are minted via globals(); "
                            f"the oracle run must collect it)")
                    else:
                        problems.append(msg)
    if rubric_file(src):
        # the file the verifier runs is compiled from this one; anything the compiler
        # would refuse is refused here, before a build and a paid run
        _, judge_problems = compile_judge(rubric_file(src))
        problems += [f"tests/{rubric_file(src).name}: {p}" for p in judge_problems]
    # the verifier is the harness's; an author copy is skipped at build, so say so here
    from .build import HARNESS_VERIFIER
    shipped = sorted(p.name for p in (src / "tests").iterdir()
                     if p.name in HARNESS_VERIFIER) if (src / "tests").is_dir() else []
    if shipped:
        say(f"WARNING tests/ ships verifier files the build will not use: {', '.join(shipped)}")

    # A floating tag rebuilds into a different image months later, so a run can
    # never be reproduced and two runs cannot be compared.
    dockerfile = src / "environment" / "Dockerfile"
    if dockerfile.is_file():
        for line in dockerfile.read_text().splitlines():
            if line.strip().upper().startswith("FROM ") and "@sha256:" not in line:
                problems.append(f"environment/Dockerfile is not pinned to a digest: "
                                f"{line.strip()[:60]}")

    # A Mac's default filesystem ignores case, a Linux one does not: a COPY whose source
    # is spelled differently from the file on disk builds here and fails on the EC2.
    problems += _copy_case_mismatches(src / "environment")

    # the author's server patches: only server code is copied over the vendor tree
    patch_problems, patch_warnings = server_patches.check(src)
    problems += patch_problems
    for line in patch_warnings:
        say(f"WARNING {line}")
    for rel in server_patches.patches(src):
        if server_patches.allowed(rel):
            say(f"server patch: environment/light-servers/{rel}")

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
    paperwork = gates.provenance(data) + gates.contamination_screen(src, data)
    if STRICT_PROVENANCE:
        problems += paperwork
    else:
        for line in paperwork:
            say(f"WARNING (not enforced; set ATLAS_STRICT_PROVENANCE=1 to refuse): {line}")
    problems += gates.collision(src, BUILD_DIR.parent / "task_registry.json")

    if problems:
        raise Failure("input folder does not meet the task contract:\n  - "
                      + "\n  - ".join(problems))
    say(f"input ok: {src.name}")
    return data
