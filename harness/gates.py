"""The CRUCIBLE-shaped gates a task input has to pass.

Each function returns a list of problems, empty when the gate is satisfied.
They are separate from validate.py's structural checks because these are about
what a bundle may claim and what it must not leak, not about whether its files
parse.

One thing these gates cannot do is establish that a task is uncontaminated. A
screen run against a root proves only that the named screen ran against that
named root; a root cannot cover a corpus it does not contain. Where a required
root is absent the gate refuses rather than passes - a refusal and a pass are
not the same answer, and only the refusal is honest.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# how much of the solution's wording may reappear in what the agent can see
SHINGLE = 8                  # words per shingle
CONTAINMENT_LIMIT = 0.02     # 2% of the solution's shingles

SOLUTION_REQUIRED = ("solve.py", "gt_env.json", "TRUTH.md")
# the files that carry how the answer was reached, in words a leak would reuse
CONTAINMENT_SOURCES = ("TRUTH.md", "solve.py", "solve.sh")
AGENT_VISIBLE = ("instruction.md", "task.toml", "data", "world_data")

# files that hold the answer; none of them may appear where the agent reads
PRIVATE_NAMES = ("gt_env.json", "oracle.json", "TRUTH.md", "solve.py", "solve.sh")

_WORD = re.compile(r"[A-Za-z0-9']+")
_TEXT_SUFFIXES = {".md", ".txt", ".json", ".toml", ".csv", ".html", ".htm", ".yaml",
                  ".yml", ".py", ".sh", ".xml", ".tsv"}


# --------------------------------------------------------------- B-2 boundary
def private_boundary(src: Path) -> list[str]:
    """Nothing that carries the answer may sit where the agent can read it."""
    problems = []
    for name in PRIVATE_NAMES:
        for area in ("data", "world_data"):
            for hit in (src / area).rglob(name) if (src / area).is_dir() else []:
                problems.append(f"{hit.relative_to(src)} would ship to the agent; "
                                f"keep it in solution/")
    # ... and the instruction must not name them either
    instruction = src / "instruction.md"
    if instruction.is_file():
        text = instruction.read_text(errors="ignore")
        for name in PRIVATE_NAMES:
            if name in text:
                problems.append(f"instruction.md mentions {name}, which is private to the grader")
    return problems


def canary_binding(src: Path) -> list[str]:
    """The instruction's canary must be the one this task recorded.

    A copied instruction brings the donor task's GUID with it, which makes a leak
    trace back to the wrong bundle. The identity file is the record; the
    instruction is the copy that has to agree with it.
    """
    identity_path = src / ".identity.json"
    instruction = src / "instruction.md"
    if not instruction.is_file():
        return []
    found = re.findall(r"instruction-canary\s+([0-9a-fA-F-]{36})",
                       instruction.read_text(errors="ignore"))
    if not identity_path.is_file():
        return [] if not found else []          # first build mints and records it
    try:
        identity = json.loads(identity_path.read_text())
    except (OSError, ValueError) as exc:
        return [f".identity.json does not parse: {exc}"]
    recorded = identity.get("instruction_canary")
    if recorded and found and recorded not in found:
        return [f"instruction.md carries canary {found[0]}, but this task recorded "
                f"{recorded}; a copied instruction traces a leak to the wrong task"]
    if len(set(found)) > 1:
        return [f"instruction.md carries {len(set(found))} different canaries: {sorted(set(found))}"]
    return []


def _shingles(text: str, n: int = SHINGLE) -> set[tuple[str, ...]]:
    words = [w.lower() for w in _WORD.findall(text)]
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def _read_text_tree(root: Path) -> str:
    if root.is_file():
        return root.read_text(errors="ignore") if root.suffix in _TEXT_SUFFIXES else ""
    parts = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in _TEXT_SUFFIXES:
            try:
                parts.append(p.read_text(errors="ignore"))
            except OSError:
                continue
    return "\n".join(parts)


def containment(src: Path, limit: float = CONTAINMENT_LIMIT) -> list[str]:
    """How much of the solution's reasoning already sits in what the agent sees.

    Word shingles, not exact-file matching: an answer pasted into a note or a
    seeded record does not arrive as a copied file. The limit is a threshold, so
    it catches wholesale reuse and not a shared turn of phrase; sub-threshold
    overlap is exactly what a deterministic check cannot see.

    Only the prose carriers are compared. gt_env.json is the world the oracle
    leaves behind, so it restates the seed world by construction - scanning it
    against world_data/ measures that construction, not a leak.
    """
    solution = src / "solution"
    if not solution.is_dir():
        return []
    private = _shingles("\n".join(
        (solution / name).read_text(errors="ignore")
        for name in CONTAINMENT_SOURCES if (solution / name).is_file()))
    if not private:
        return []
    visible = _shingles("\n".join(_read_text_tree(src / part) for part in AGENT_VISIBLE
                                  if (src / part).exists()))
    shared = private & visible
    ratio = len(shared) / len(private)
    if ratio > limit:
        sample = " ".join(sorted(shared)[0]) if shared else ""
        return [f"{ratio:.1%} of solution/ wording ({len(shared)} of {len(private)} shingles) "
                f"also appears in what the agent can read, over the {limit:.0%} limit; "
                f"e.g. \"{sample}\""]
    return []


# ------------------------------------------------------------- B-1 provenance
PROVENANCE_FIELDS = ("origin", "train_cutoff", "license")


def provenance(meta: dict) -> list[str]:
    """Every task carries one upstream_provenance record.

    The harness can require the record and check its shape. It cannot verify the
    claim: whether a source really is what an author says it is, and whether a
    cutoff date is true, are facts from outside this repository.
    """
    record = (meta.get("task") or {}).get("metadata", {}).get("upstream_provenance")
    if not record:
        return ["task.toml [task.metadata] has no upstream_provenance record: it needs "
                "origin (where the material came from), train_cutoff (YYYY-MM-DD, the "
                "per-instance cutoff this task is safe against) and license"]
    if not isinstance(record, dict):
        return ["task.toml upstream_provenance must be a table"]
    problems = [f"upstream_provenance has no {f}" for f in PROVENANCE_FIELDS if not record.get(f)]
    cutoff = str(record.get("train_cutoff") or "")
    if cutoff and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
        problems.append(f"upstream_provenance train_cutoff {cutoff!r} is not YYYY-MM-DD")
    return problems


# ---------------------------------------------------- B-3 contamination screen
SCREEN_ROOTS = ("exclusion_list", "freeze_table", "near_duplicate_index")


def contamination_screen(src: Path, meta: dict) -> list[str]:
    """Refuse unless a screening record names its instrument, roots and date.

    The screens themselves belong to CRUCIBLE and read ENGRAM-owned roots this
    harness does not hold, and a contract that forbids trusting an author's
    self-scan cannot be discharged by a check the author's own harness runs. So
    this gate records rather than screens: a bundle with no record, or one whose
    roots were unresolvable, is refused rather than passed.
    """
    record = (meta.get("task") or {}).get("metadata", {}).get("contamination_screen")
    if not record:
        return ["task.toml [task.metadata] has no contamination_screen record: it needs "
                "instrument, screened_on (YYYY-MM-DD) and a roots table naming "
                f"{', '.join(SCREEN_ROOTS)}; an absent root is recorded as "
                "'unresolved', never as a pass"]
    if not isinstance(record, dict):
        return ["task.toml contamination_screen must be a table"]
    problems = []
    for field in ("instrument", "screened_on"):
        if not record.get(field):
            problems.append(f"contamination_screen has no {field}")
    roots = record.get("roots") or {}
    for root in SCREEN_ROOTS:
        state = str(roots.get(root) or "").strip().lower()
        if not state:
            problems.append(f"contamination_screen roots has no {root}: name the root it "
                            f"resolved against, or 'unresolved'")
        elif state == "unresolved":
            problems.append(f"contamination_screen {root} is unresolved, so this task is "
                            f"not screened against it; the screen refuses rather than passes")
    return problems


# --------------------------------------------------------------- H-4 collision
def _fingerprint(text: str) -> str:
    import hashlib
    words = [w.lower() for w in _WORD.findall(text)]
    return hashlib.sha256(" ".join(words).encode()).hexdigest()[:16]


def collision(src: Path, registry: Path) -> list[str]:
    """Two tasks stating the same problem are one task counted twice."""
    instruction = src / "instruction.md"
    if not instruction.is_file():
        return []
    fp = _fingerprint(instruction.read_text(errors="ignore"))
    uuid = ""
    if (src / ".identity.json").is_file():
        try:
            uuid = json.loads((src / ".identity.json").read_text()).get("uuid", "")
        except (OSError, ValueError):
            pass
    known = {}
    if registry.is_file():
        try:
            known = json.loads(registry.read_text())
        except (OSError, ValueError):
            known = {}
    clash = [u for u, f in known.items() if f == fp and u != uuid]
    known[uuid or src.name] = fp
    try:
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps(known, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass
    return [f"instruction.md is word-for-word the statement already registered for "
            f"{clash[0]}"] if clash else []
