"""Paths, config and small helpers shared by the harness steps."""
from __future__ import annotations

import json
import os
import re
import shutil
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
    full_env = {**os.environ, **(env or {})}
    if not capture and _LOG_FILE is not None and sys.stdout.isatty():
        returncode = _run_on_pty(cmd, cwd, full_env, shell)
        proc = subprocess.CompletedProcess(cmd, returncode, None, None)
    elif not capture and _LOG_FILE is not None:
        popen = subprocess.Popen(
            cmd, cwd=cwd, shell=shell, text=True, env=full_env,
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


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _final_frame(raw: bytes) -> str:
    """The text a terminal would finally show for one line of pty output.

    A pty turns each newline into \\r\\n, and a redrawn line arrives as frames
    separated by \\r. So the trailing \\r goes first, then the last non-empty
    frame is what was left on screen.
    """
    text = _ANSI.sub("", raw.decode(errors="replace")).rstrip("\r")
    frames = [f.rstrip() for f in text.split("\r") if f.strip()]
    return frames[-1] if frames else ""


def _run_on_pty(cmd, cwd, env, shell) -> int:
    """Run `cmd` on a pseudo-terminal: live output on screen, clean text in the log.

    Harbor draws trial progress with rich, which renders live bars only when its
    output is a terminal. Piping it into the log made rich switch them off, so a
    run showed nothing between start and finish. On a pty rich sees a terminal
    and draws as it would bare; the bytes go to the screen unchanged, and the
    log gets them with colour codes and cursor redraws removed, keeping only the
    final text of each redrawn line and dropping repeats.
    """
    import fcntl, pty, select, struct, termios
    master, slave = pty.openpty()
    cols, rows = shutil.get_terminal_size((160, 48))
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    popen = subprocess.Popen(cmd, cwd=cwd, shell=shell, env=env, stdin=subprocess.DEVNULL,
                             stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    pending, last_logged = b"", ""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.5)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:          # the child closed its side: EIO on Linux
                    data = b""
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
                pending += data
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    text = _final_frame(raw)
                    if text and text != last_logged:
                        _mirror(text)
                        last_logged = text
            elif popen.poll() is not None:
                break
    finally:
        os.close(master)
    tail = _final_frame(pending)
    if tail and tail != last_logged:
        _mirror(tail)
    return popen.wait()


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
