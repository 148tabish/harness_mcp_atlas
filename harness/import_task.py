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

from .common import ROOT, TEMPLATES, die, say, write_json

# The harness supplies these. An import drops the ones that match the harness
# copy byte for byte and keeps any the task changed deliberately, because the
# build prefers whatever the input folder carries.
GENERATED_TESTS = {"Dockerfile", "Dockerfile.ecr", "docker-compose.yaml", "evaluate.sh",
                   "grade.py", "test.sh", "claims_lib.py", "ctrf_pytest_plugin.py", "_judge"}


def _same_as_template(item: Path) -> bool:
    mine = TEMPLATES / "tests" / item.name
    if item.is_dir() or not mine.is_file():
        return item.is_dir() and mine.is_dir() and _dir_same(item, mine)
    return item.read_bytes() == mine.read_bytes()


def _dir_same(a: Path, b: Path) -> bool:
    names = {p.name for p in a.iterdir()} | {p.name for p in b.iterdir()}
    for name in names:
        x, y = a / name, b / name
        if not (x.is_file() and y.is_file() and x.read_bytes() == y.read_bytes()):
            return False
    return True


def _task_blocks(raw: str) -> str:
    lines = raw.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "[task]")
    stop = next((i for i, ln in enumerate(lines[start:], start)
                 if ln.strip() in ("[agent]", "[verifier]", "[environment]")), len(lines))
    return "\n".join(lines[start:stop]).rstrip() + "\n"


def _with_wired_servers(blocks: str, raw: str) -> str:
    """Record every server the task wires, not just the graded apps.

    `apps` names the graded ones; a task usually wires more, the ungraded
    distractors that make the search real. Without this the rebuild would drop
    them and the task would be easier than the one that was authored.
    """
    wired = [ln.split('"')[1] for ln in raw.splitlines()
             if ln.startswith("name = ") and '"Light' in ln]
    if not wired or "mcp_servers = " in blocks:
        return blocks
    listing = "[" + ", ".join(f'"{w}"' for w in wired) + "]"
    note = ("# every server wired into the environment: the graded apps above plus\n"
            "# the ungraded distractors\n")
    return re.sub(r"^(apps = .*)$", lambda m: f"{m.group(1)}\n{note}mcp_servers = {listing}",
                  blocks, count=1, flags=re.M)


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
    # schema_version sits above [task], so the block extraction alone would drop
    # the contract version the task declares
    version = re.search(r'^schema_version\s*=\s*"[^"]+"', raw, re.M)
    header = (version.group(0) + "\n\n") if version else ""
    (dest / "task.toml").write_text(header + _with_wired_servers(_task_blocks(raw), raw))
    shutil.copy2(src / "instruction.md", dest / "instruction.md")

    (dest / "environment").mkdir()
    shutil.copy2(src / "environment" / "Dockerfile", dest / "environment" / "Dockerfile")

    ls = src / "environment" / "light-servers"
    shutil.copytree(ls / "task_data", dest / "data")
    shutil.copytree(ls / "world_data", dest / "world_data")

    (dest / "tests").mkdir()
    kept_own = []
    for item in sorted((src / "tests").iterdir()):
        if item.name == "__pycache__":
            continue
        if item.name in GENERATED_TESTS:
            if _same_as_template(item):
                continue
            kept_own.append(item.name)
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, dest / "tests" / item.name)
    if kept_own:
        say(f"kept this task's own {', '.join(kept_own)} (differs from the harness copy)")
    shutil.copytree(src / "solution", dest / "solution",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # keep the task's identity so a rebuild lands on the same uuid and canary
    canary = re.search(r"harbor-canary GUID ([0-9a-f-]{36})", raw)
    # the uuid is the last one in the task name, which may carry a slug before
    # it (atlas/Author_some-task_<uuid>); matching only atlas/<uuid> missed those
    # and the build then minted a fresh uuid and canary for an existing task
    name_line = re.search(r'^name = "([^"]*)"', raw, re.M)
    found = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                       name_line.group(1)) if name_line else []
    uuid_m = re.match(r"(.+)", found[-1]) if found else None
    if canary and uuid_m:
        write_json(dest / ".identity.json",
                   {"uuid": uuid_m.group(1), "canary": canary.group(1), "imported_from": str(src)})

    kept = sorted(p.name for p in (dest / "tests").iterdir())
    say(f"imported {src.name} -> input/{args.name} (tests kept: {kept})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
