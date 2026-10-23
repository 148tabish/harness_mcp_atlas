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

from . import server_patches
from .build import HARNESS_BLOCKS, HARNESS_VERIFIER
from .common import ROOT, die, say, write_json
from .judge_compile import decompile_toml

# The verifier is the harness's and the build never takes an author copy of it (see
# build.HARNESS_VERIFIER), so an import leaves those files behind instead of carrying
# a stale verifier that validate would then warn about on every run.
GENERATED_TESTS = set(HARNESS_VERIFIER)


def _task_blocks(raw: str) -> str:
    """The author's [task] and [task.metadata] text, up to the first harness-owned block.

    A built task.toml carries the generated [[environment.mcp_servers]] list after the
    author's blocks (and may carry nothing else); stopping only at [agent] / [verifier] /
    [environment] copied that list into the input, which validate then refused.
    """
    lines = raw.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "[task]")
    stop = next((i for i, ln in enumerate(lines[start:], start)
                 if ln.strip() in HARNESS_BLOCKS), len(lines))
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
    # server code the author changed travels as a patch; a corpus or data difference
    # does not (the build cannot reproduce it: world changes belong in world_data/)
    carried, left = server_patches.extract(ls, dest)
    if carried:
        say(f"server patches carried: {', '.join(carried)}")
    for rel in left:
        say(f"WARNING {rel} differs from vendor/light-servers and is not server code; "
            f"the rebuilt task serves the vendor copy")

    (dest / "tests").mkdir()
    dropped = []
    for item in sorted((src / "tests").iterdir()):
        if item.name == "__pycache__":
            continue
        if item.name in GENERATED_TESTS:
            dropped.append(item.name)
            # an author module that only shares the harness library's name is not the
            # harness's: dropping it silently leaves test_outputs.py importing the wrong API
            # (310e65cb shipped its own claims_lib.py: load_fixture(env, name), end_env(),
            # captured(), by_key(); the harness's has none of those)
            template = ROOT / "templates" / "tests" / item.name
            # only code modules: the stamped files (test.sh, Dockerfile, compose) always differ
            if item.suffix == ".py" and item.is_file() and template.is_file() \
                    and item.read_bytes() != template.read_bytes():
                say(f"WARNING tests/{item.name} differs from the harness's templates/tests/{item.name} "
                    f"and is dropped; if test_outputs.py relies on the author's version, carry it "
                    f"under another name (e.g. tests/task_lib.py) and change the import")
            continue
        if item.is_dir():
            shutil.copytree(item, dest / "tests" / item.name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, dest / "tests" / item.name)
    if dropped:
        say(f"left the built task's verifier behind (the harness supplies its own): "
            f"{', '.join(dropped)}")
    if (dest / "tests" / "judge.toml").is_file() and (dest / "tests" / "rubric.json").is_file():
        say("WARNING tests/judge.toml and tests/rubric.json both present; validate will refuse "
            "until one is removed (the task's own verifier used rubric.json)")
    # a task built since 2026-10-22 ships the rubric only in its compiled form
    compiled = src / "tests" / "reward" / "rubric" / "judge.toml"
    if compiled.is_file() and not any((dest / "tests" / n).is_file() for n in ("judge.toml", "rubric.json")):
        (dest / "tests" / "judge.toml").write_text(decompile_toml(compiled.read_text()))
        say("tests/judge.toml reconstructed from the compiled rubric")
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
