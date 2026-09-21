"""Paths, config and small helpers shared by the harness steps."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"
TEMPLATES = ROOT / "templates"
OUT = ROOT / "out"
BUILD_DIR = OUT / "build"
JOBS_DIR = OUT / "jobs"
PACKAGED_DIR = OUT / "packaged"
LOGS_DIR = OUT / "logs"
CONFIG = ROOT / "config"

# every line the harness prints, and everything harbor prints, is mirrored here
_LOG_FILE: "TextIO | None" = None

# the one venv the harness runs harbor from
HARBOR_BIN = ROOT / ".venv" / "bin" / "harbor"


class Failure(Exception):
    """Anything that should stop the run with a readable message."""


def open_log(name: str) -> Path:
    """Start mirroring output into out/logs/<name>.log and return its path."""
    global _LOG_FILE
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"{name}.log"
    _LOG_FILE = path.open("a", buffering=1)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _LOG_FILE.write(f"\n===== {stamp}  {' '.join(sys.argv)}\n")
    return path


def _mirror(line: str) -> None:
    if _LOG_FILE is not None:
        _LOG_FILE.write(line if line.endswith("\n") else line + "\n")


def say(msg: str) -> None:
    line = f"[harness] {msg}"
    print(line, flush=True)
    _mirror(line)


def load_models() -> dict:
    """config/models.yaml without a yaml dependency in the outer venv."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        yaml = None
    text = (CONFIG / "models.yaml").read_text()
    if yaml is not None:
        return yaml.safe_load(text)
    return _mini_yaml(text)


def _mini_yaml(text: str) -> dict:
    """Enough YAML for config/models.yaml: nested maps of scalars, two levels deep."""
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, _, value = raw.strip().partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        value = value.strip()
        if value == "":
            node: dict = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            if value in ("true", "false"):
                parsed: object = value == "true"
            elif re.fullmatch(r"-?\d+", value):
                parsed = int(value)
            else:
                parsed = value.strip('"').strip("'")
            parent[key] = parsed
    return root


def run(cmd: list[str] | str, cwd: Path | None = None, env: dict | None = None,
        check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a command, streaming its output unless `capture` is asked for.

    While a log is open the streamed output is mirrored into it line by line, so
    a run's terminal and its .log carry the same thing.
    """
    shell = isinstance(cmd, str)
    if not capture and _LOG_FILE is not None:
        popen = subprocess.Popen(
            cmd, cwd=cwd, shell=shell, text=True, env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        for line in popen.stdout:                       # type: ignore[union-attr]
            sys.stdout.write(line)
            sys.stdout.flush()
            _mirror(line)
        popen.wait()
        proc = subprocess.CompletedProcess(cmd, popen.returncode, None, None)
    else:
        proc = subprocess.run(
            cmd, cwd=cwd, shell=shell, check=False, text=True,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
    if check and proc.returncode != 0:
        where = f" in {cwd}" if cwd else ""
        out = f"\n{proc.stdout}" if capture and proc.stdout else ""
        raise Failure(f"command failed (rc={proc.returncode}){where}: "
                      f"{cmd if shell else ' '.join(cmd)}{out}")
    return proc


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines from a .env, ignoring comments and blanks."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def die(msg: str) -> None:
    print(f"[harness] ERROR: {msg}", file=sys.stderr, flush=True)
    _mirror(f"[harness] ERROR: {msg}")
    raise SystemExit(1)
