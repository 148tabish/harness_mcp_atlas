"""An author's changes to the Light servers' code, carried with the task.

A task may change how a server behaves while the task is worked (5b10675f's Airtable
answers preview rows, re-delivers counts and hands sources over once the desk starts
raising limits; its author calls airtable.py "the one server file a task may change").
Those files live in the input at

    input/<name>/environment/light-servers/software/<App>/<file>

mirroring their path under vendor/light-servers, and the build copies them over the
vendor copy after it lays the light servers down. Only server code travels this way:
*.py files and desc.json under software/. A world or corpus change (corpus/*.yaml,
task_data) is not a patch; it goes through world_data/, the overlay the servers seed
from, so the import reports such a difference and leaves it behind.
"""
from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

from .common import VENDOR

PATCH_ROOT = Path("environment") / "light-servers"
ALLOWED_SUFFIXES = {".py"}
ALLOWED_NAMES = {"desc.json"}
IGNORED_DIRS = {"__pycache__", "task_data", "world_data"}


def allowed(rel: Path) -> bool:
    """Server code under software/: what the build will copy over the vendor tree."""
    return (len(rel.parts) > 1 and rel.parts[0] == "software"
            and (rel.suffix in ALLOWED_SUFFIXES or rel.name in ALLOWED_NAMES))


def patches(src: Path) -> list[Path]:
    """Relative paths of every file the input carries under environment/light-servers."""
    root = src / PATCH_ROOT
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root) for p in root.rglob("*")
                  if p.is_file() and not set(p.relative_to(root).parts) & IGNORED_DIRS)


def check(src: Path) -> tuple[list[str], list[str]]:
    """(problems, warnings) for the patches an input carries."""
    problems, warnings = [], []
    for rel in patches(src):
        if not allowed(rel):
            problems.append(f"environment/light-servers/{rel} is not server code; only *.py and "
                            f"desc.json under software/ travel as patches (world changes go in "
                            f"world_data/)")
            continue
        vendor = VENDOR / "light-servers" / rel
        if vendor.is_file() and filecmp.cmp(src / PATCH_ROOT / rel, vendor, shallow=False):
            warnings.append(f"environment/light-servers/{rel} is identical to the vendor copy")
    return problems, warnings


def apply(src: Path, light_servers: Path) -> list[str]:
    """Copy the input's patches over the laid-down vendor tree; say what each one did."""
    done = []
    for rel in patches(src):
        if not allowed(rel):
            continue           # validate refuses these; a build on its own skips them
        target = light_servers / rel
        what = "replaces vendor" if target.is_file() else "new file"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / PATCH_ROOT / rel, target)
        done.append(f"{rel} ({what})")
    return done


def extract(task_light_servers: Path, dest: Path) -> tuple[list[str], list[str]]:
    """From a built task's light-servers, carry what differs from vendor into an input.

    Returns (carried, left_behind): server code that differs or is new is copied under
    dest/environment/light-servers; any other difference (corpus, data) is named so the
    importer can see it, since the build would not reproduce it.
    """
    carried, left = [], []
    software = task_light_servers / "software"
    if not software.is_dir():
        return carried, left
    for p in sorted(software.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(task_light_servers)
        if set(rel.parts) & IGNORED_DIRS or p.suffix == ".pyc":
            continue
        vendor = VENDOR / "light-servers" / rel
        if vendor.is_file() and filecmp.cmp(p, vendor, shallow=False):
            continue
        if allowed(rel):
            target = dest / PATCH_ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            carried.append(str(rel))
        else:
            left.append(str(rel))
    return carried, left
