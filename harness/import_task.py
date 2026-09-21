"""Turn an already-built harbor task back into an input folder.

    python -m harness.import_task --from ../fixed-tasks/<uuid> --name wraysbury

Keeps the task-specific half (instruction, [task] blocks, evidence, world
overlay, tests, solution, agent Dockerfile) and drops everything the harness
generates, so the result rebuilds to the same task through the normal path.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from .common import ROOT, die, say, write_json

# the harness owns these; an imported folder must not carry them
GENERATED_TESTS = {"Dockerfile", "Dockerfile.ecr", "docker-compose.yaml", "evaluate.sh",
                   "grade.py", "test.sh", "claims_lib.py", "ctrf_pytest_plugin.py", "_judge"}


def _task_blocks(raw: str) -> str:
    lines = raw.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "[task]")
    stop = next((i for i, ln in enumerate(lines[start:], start)
                 if ln.strip() in ("[agent]", "[verifier]", "[environment]")), len(lines))
    return "\n".join(lines[start:stop]).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness.import_task", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", required=True, type=Path, help="a built task folder")
    ap.add_argument("--name", required=True, help="input folder name to create")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    src = args.src.resolve()
    dest = ROOT / "input" / args.name
    if not (src / "task.toml").is_file():
        die(f"{src} has no task.toml")
    if dest.exists():
        if not args.force:
            die(f"{dest} exists; pass --force")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    raw = (src / "task.toml").read_text()
    (dest / "task.toml").write_text(_task_blocks(raw))
    shutil.copy2(src / "instruction.md", dest / "instruction.md")

    (dest / "environment").mkdir()
    shutil.copy2(src / "environment" / "Dockerfile", dest / "environment" / "Dockerfile")

    ls = src / "environment" / "light-servers"
    shutil.copytree(ls / "task_data", dest / "data")
    shutil.copytree(ls / "world_data", dest / "world_data")

    (dest / "tests").mkdir()
    for item in sorted((src / "tests").iterdir()):
        if item.name in GENERATED_TESTS or item.name == "__pycache__":
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, dest / "tests" / item.name)
    shutil.copytree(src / "solution", dest / "solution",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # keep the task's identity so a rebuild lands on the same uuid and canary
    canary = re.search(r"harbor-canary GUID ([0-9a-f-]{36})", raw)
    uuid_m = re.search(r'name = "atlas/([0-9a-f-]{36})"', raw)
    if canary and uuid_m:
        write_json(dest / ".identity.json",
                   {"uuid": uuid_m.group(1), "canary": canary.group(1), "imported_from": str(src)})

    kept = sorted(p.name for p in (dest / "tests").iterdir())
    say(f"imported {src.name} -> input/{args.name} (tests kept: {kept})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
